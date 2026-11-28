"""Verify daily limits on independent Rust workers sharing a fixture database."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import socket
from socketserver import StreamRequestHandler, ThreadingTCPServer
import subprocess
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Thread
from time import time

from bs4 import BeautifulSoup
from pymemcache.client.base import Client
from pymemcache.serde import pickle_serde
import requests

from agent_write_parity import fixture_database
from compare import HOST, ROOT, RUST_BIN, available_port, start_server

SEED = """
from reference import application
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sites.models import Site
from django.db import connection
from core.models import Debater, School, SchoolAdmin, User
from uuid import uuid4
import json

# Use an isolated cache key even if another test shares this Memcached instance.
user_id = 1_000_000_000 + uuid4().int % 1_000_000_000
username = f"daily-quota-{user_id}"
User.objects.create_user(username=f"{username}-login", password="fixture-local-password")
user = User.objects.create_user(id=user_id, username=username)
school = School.objects.create(name="Daily quota fixture school", short_name="DQF")
SchoolAdmin.objects.create(user=user, school=school, primary=True)
debater_ids = [
    Debater.objects.create(school=school, first_name="Claimable", last_name=f"Person {i}").pk
    for i in range(7)
]
rejected = Debater.objects.create(school=school, first_name="Commit", last_name="Rejected")
with connection.cursor() as cursor:
    cursor.execute(
        "CREATE TABLE quota_fixture_reference (debater_id INTEGER REFERENCES core_debater(id) "
        "DEFERRABLE INITIALLY DEFERRED)"
    )
    tests = [
        ("core_debater", "NEW.first_name='Created' AND NEW.last_name='Person -1'"),
        ("core_claimdebaterrequest", f"NEW.debater_id={rejected.pk}"),
    ]
    if connection.vendor == "postgresql":
        cursor.execute(
            "CREATE FUNCTION reject_quota_fixture() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN INSERT INTO quota_fixture_reference VALUES (-1); RETURN NEW; END; $$"
        )
    for table, condition in tests:
        prefix = f"CREATE TRIGGER {table}_quota_fixture AFTER INSERT ON {table}"
        clause = (f"FOR EACH ROW WHEN ({condition}) EXECUTE FUNCTION reject_quota_fixture()"
                  if connection.vendor == "postgresql" else
                  f"WHEN {condition} BEGIN INSERT INTO quota_fixture_reference VALUES (-1); END")
        cursor.execute(f"{prefix} {clause}")
session = SessionStore()
session.update({"_auth_user_id": str(user.pk), "_auth_user_backend": "django.contrib.auth.backends.ModelBackend",
                "_auth_user_hash": user.get_session_auth_hash()})
session.save()
print(json.dumps([school.id, debater_ids, session.session_key, username, Site.objects.get(pk=1).domain, rejected.pk]))
"""

COUNTS = """
from reference import application
from core.models import ClaimDebaterRequest, Debater
from sys import argv
import json
school = int(argv[1])
print(json.dumps([
    Debater.objects.filter(school_id=school, first_name="Created").count(),
    ClaimDebaterRequest.objects.filter(debater__school_id=school).count(),
]))
"""


class RefuseQuotaWrites(StreamRequestHandler):
    """Allow cache reads while simulating a cache that cannot store new values."""

    def handle(self):
        fields = self.rfile.readline(300).split()
        if fields and fields[0] == b"get":
            self.wfile.write(b"END\r\n")
        elif len(fields) == 5 and fields[0] in (b"add", b"replace"):
            self.rfile.read(int(fields[4]) + 2)
            self.wfile.write(b"SERVER_ERROR out of memory\r\n")


@contextmanager
def refusing_cache():
    with ThreadingTCPServer((HOST, 0), RefuseQuotaWrites) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            yield f"{HOST}:{server.server_address[1]}"
        finally:
            server.shutdown()
            thread.join()


class InterruptedCache(StreamRequestHandler):
    def handle(self):
        if self.server.interrupted.is_set():
            return
        line = self.rfile.readline(300)
        fields = line.split()
        if not fields:
            return
        with socket.create_connection(self.server.upstream, timeout=2) as backend, backend.makefile("rb") as reader:
            if fields[0] in (b"add", b"cas", b"replace", b"set"):
                line += self.rfile.read(int(fields[4]) + 2)
            backend.sendall(line)
            reply = reader.readline()
            self.wfile.write(reply)
            if reply.startswith(b"VALUE "):
                length = int(reply.split()[3])
                self.wfile.write(reader.read(length + 2))
                self.wfile.write(reader.readline())


@contextmanager
def interrupted_cache(cache):
    host, _, port = cache.rpartition(":")
    with ThreadingTCPServer((HOST, 0), InterruptedCache) as server:
        server.upstream = (host, int(port))
        server.interrupted = Event()
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            yield f"{HOST}:{server.server_address[1]}", server.interrupted
        finally:
            server.shutdown()
            thread.join()


@contextmanager
def worker(env, database, cache):
    port = available_port()
    cache_host, _, cache_port = cache.rpartition(":")
    if cache_host != HOST or not cache_port.isdecimal():
        raise ValueError("Quota tests require a local cache")
    with start_server("Rust", [str(RUST_BIN)], port, {
        **env, "BLACK_ROD_DATABASE_URL": os.environ.get(
            "BLACK_ROD_PARITY_POSTGRES_URL", f"sqlite:{database.as_posix()}?mode=rw"
        ),
        "BLACK_ROD_BIND": f"{HOST}:{port}",
        "BLACK_ROD_MEMCACHED_ADDR": cache,
        "BLACK_ROD_TRUSTED_PROXY_IP": HOST,
        "LIVE_MITTAB_DIRECTORY_URL": "",
    }) as server:
        yield f"http://{HOST}:{server.port}"


def forms(url, session, school):
    cookie = {"sessionid": session}
    create = f"/core/school-admin/{school}/debater/create/"
    claim = "/core/claim-debater/request/"
    response = requests.get(url + create, cookies=cookie, timeout=15)
    assert response.status_code == 200, response.status_code
    cookie.update(response.cookies.get_dict())
    token = BeautifulSoup(response.content, "html.parser").select_one('input[name=csrfmiddlewaretoken]')["value"]

    def add(index):
        return requests.post(url + create, cookies=cookie, timeout=15, allow_redirects=False, data={
            "csrfmiddlewaretoken": token, "first_name": "Created", "last_name": f"Person {index}",
            "status": "1", "first_season": "2025", "latest_season": "2026",
        })

    def submit(debater):
        return requests.post(url + claim, cookies=cookie, timeout=15, allow_redirects=False, data={
            "csrfmiddlewaretoken": token, "school": school, "debater": debater,
        })

    return add, submit


def check_login_limits(roots, cache, username, site):
    path = "/accounts/login/"
    suffix = sha256(username.encode()).hexdigest()[:8]
    headers = {"X-Forwarded-For": f"2001:db8:{suffix[:4]}:{suffix[4:]}::1"}
    credentials = []
    for root in roots:
        response = requests.get(root + path, timeout=15)
        assert response.status_code == 200, response.status_code
        token = BeautifulSoup(response.content, "html.parser").select_one('input[name=csrfmiddlewaretoken]')["value"]
        credentials.append((root + path, response.cookies.get_dict(), token))

    def post(number, identity, password, barrier):
        url, cookies, token = credentials[number % len(credentials)]
        barrier.wait(timeout=10)
        return requests.post(url, cookies=cookies, headers=headers, timeout=20, allow_redirects=False, data={
            "login": identity, "password": password, "csrfmiddlewaretoken": token,
        })

    def batch(identity, passwords):
        barrier = Barrier(len(passwords))
        with ThreadPoolExecutor(max_workers=len(passwords)) as pool:
            tasks = [pool.submit(post, i, identity, password, barrier) for i, password in enumerate(passwords)]
            return [task.result() for task in tasks]

    host, _, port = cache.rpartition(":")
    with closing(Client((host, int(port)), serde=pickle_serde, connect_timeout=2, timeout=2)) as backend:
        def key(identity):
            digest = sha256(f"{site}:{identity}".encode()).hexdigest()
            return f":1:allauth:rl:login_failed:{digest}"

        valid = f"{username}-login"
        prior = time()
        assert backend.set(key(valid), [prior], expire=300, noreply=False)
        results = batch(valid, ["fixture-local-password", "unused-password", "unused-password", "unused-password"])
        assert sorted(result.status_code for result in results) == [200] * 3 + [302]
        remaining = backend.get(key(valid))
        assert len(remaining) == 4 and prior in remaining, "Login rollback discarded another attempt"

        unknown = f"{username}-unknown"
        results = batch(unknown, ["unused-password"] * 6)
        assert sum("Too many failed login attempts" in result.text for result in results) == 1
        assert len(backend.get(key(unknown))) == 5, "Workers did not share the five-attempt limit"


def check_interrupted_logins(url, cache, username, site, interruption):
    path = url + "/accounts/login/"
    username += "-interrupted"
    headers = {"X-Forwarded-For": "2001:db8:6789:abcd::1"}
    initial = requests.get(path, timeout=15)
    token = BeautifulSoup(initial.content, "html.parser").select_one("input[name=csrfmiddlewaretoken]")["value"]
    data = {"login": username, "password": "fixture-wrong-password", "csrfmiddlewaretoken": token}

    def post():
        response = requests.post(path, cookies=initial.cookies, headers=headers, data=data, timeout=15)
        assert response.status_code == 200, response.status_code
        return response.text

    for _ in range(2):
        assert "Too many failed login attempts" not in post()
    interruption.set()
    for _ in range(3):
        assert "Too many failed login attempts" not in post()
    interruption.clear()
    assert "Too many failed login attempts" in post(), "Restoring Memcached discarded local attempts"

    host, _, port = cache.rpartition(":")
    with closing(Client((host, int(port)), serde=pickle_serde, connect_timeout=2, timeout=2)) as backend:
        digest = sha256(f"{site}:{username}".encode()).hexdigest()
        attempts = backend.get(f":1:allauth:rl:login_failed:{digest}")
        assert len(attempts) == 5, attempts


def exercise(env, database, cache, school, debaters, session, username, site, rejected, counts):
    with refusing_cache() as read_only:
        for failing in (f"{HOST}:{available_port()}", read_only):
            with worker(env, database, failing) as url:
                add, submit = forms(url, session, school)
                assert add(0).status_code == 503
                assert submit(debaters[0]).status_code == 503
                assert counts() == [0, 0]

    with interrupted_cache(cache) as (address, interruption), worker(env, database, address) as url:
        check_interrupted_logins(url, cache, username, site, interruption)

    with worker(env, database, cache) as first, worker(env, database, cache) as second:
        clients = [forms(url, session, school) for url in (first, second)]
        assert clients[0][0](-1).status_code == 500
        assert clients[1][1](rejected).status_code == 500
        assert counts() == [0, 0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            creations = [pool.submit(clients[i % 2][0], i) for i in range(7)]
            assert sorted(result.result().status_code for result in creations) == [302] * 5 + [403] * 2
            claims = [pool.submit(clients[i % 2][1], value) for i, value in enumerate(debaters)]
            responses = [result.result() for result in claims]
            assert sorted(response.status_code for response in responses) == [200] * 2 + [302] * 5
            assert all("You have reached the daily limit" in response.text
                       for response in responses if response.status_code == 200)
        assert counts() == [5, 5]
        check_login_limits([first, second], cache, username, site)


def main():
    seed = Path(os.environ["BR_STATE"]) / "dev.sqlite3"
    cache = os.environ["BLACK_ROD_PARITY_MEMCACHED_ADDR"]
    with TemporaryDirectory(prefix="standings-write-quota-") as folder:
        database = Path(folder) / "dev.sqlite3"
        postgres = os.environ.get("BLACK_ROD_PARITY_POSTGRES_URL")
        with fixture_database(seed, database, postgres):
            env = {**os.environ, "BR_STATE": folder, "SECRET_KEY": "disposable-local-parity-key",
                   "PYTHONPATH": f"{Path(__file__).resolve().parent}{os.pathsep}{os.environ['PYTHONPATH']}"}

            def fixture_query(code, *args):
                lines = subprocess.check_output(
                    [str(ROOT / "venv/bin/python"), "-c", code, *args], text=True, env=env
                ).splitlines()
                return json.loads(lines[-1])

            school, debaters, session, username, site, rejected = fixture_query(SEED)
            exercise(env, database, cache, school, debaters, session, username, site, rejected,
                     lambda: fixture_query(COUNTS, str(school)))
    print("Shared quotas withstand cache and database failures, and concurrent logins retain all attempts")


if __name__ == "__main__":
    main()

"""Verify daily limits on independent Rust workers sharing a fixture database."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
from socketserver import StreamRequestHandler, ThreadingTCPServer
import subprocess
from tempfile import TemporaryDirectory
from threading import Thread

from bs4 import BeautifulSoup
import requests

from agent_write_parity import fixture_database
from compare import HOST, ROOT, RUST_BIN, available_port, start_server

SEED = """
from reference import application
from django.contrib.sessions.backends.db import SessionStore
from core.models import Debater, School, SchoolAdmin, User
from uuid import uuid4
import json

# Use an isolated cache key even if another test shares this Memcached instance.
user_id = 1_000_000_000 + uuid4().int % 1_000_000_000
user = User.objects.create_user(id=user_id, username="daily-quota-fixture")
school = School.objects.create(name="Daily quota fixture school", short_name="DQF")
SchoolAdmin.objects.create(user=user, school=school, primary=True)
debater_ids = [
    Debater.objects.create(school=school, first_name="Claimable", last_name=f"Person {i}").pk
    for i in range(7)
]
session = SessionStore()
session.update({"_auth_user_id": str(user.pk), "_auth_user_backend": "django.contrib.auth.backends.ModelBackend",
                "_auth_user_hash": user.get_session_auth_hash()})
session.save()
print(json.dumps([school.id, debater_ids, session.session_key]))
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


def exercise(env, database, cache, school, debaters, session, counts):
    with refusing_cache() as read_only:
        for failing in (f"{HOST}:{available_port()}", read_only):
            with worker(env, database, failing) as url:
                add, submit = forms(url, session, school)
                assert add(0).status_code == 503
                assert submit(debaters[0]).status_code == 503
                assert counts() == [0, 0]

    with worker(env, database, cache) as first, worker(env, database, cache) as second:
        clients = [forms(url, session, school) for url in (first, second)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            creations = [pool.submit(clients[i % 2][0], i) for i in range(7)]
            assert sorted(result.result().status_code for result in creations) == [302] * 5 + [403] * 2
            claims = [pool.submit(clients[i % 2][1], value) for i, value in enumerate(debaters)]
            responses = [result.result() for result in claims]
            assert sorted(response.status_code for response in responses) == [200] * 2 + [302] * 5
            assert all("You have reached the daily limit" in response.text
                       for response in responses if response.status_code == 200)
        assert counts() == [5, 5]


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

            school, debaters, session = fixture_query(SEED)
            exercise(env, database, cache, school, debaters, session,
                     lambda: fixture_query(COUNTS, str(school)))
    print("Shared write quotas survive cache failure and enforce the five-write limit across workers")


if __name__ == "__main__":
    main()

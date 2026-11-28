"""Compare the Rust server with Django against the same local fixture database."""

from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import dataclass
import hashlib
from html.parser import HTMLParser
from http.client import HTTPConnection
from itertools import groupby
import json
import math
import os
from pathlib import Path
import platform
import socket
import sqlite3
import subprocess
import tempfile
from threading import Event, Thread
from time import perf_counter, sleep


HOST = "127.0.0.1"
ROOT = Path(__file__).resolve().parents[2]
TRACKED_HEADERS = ("content-type", "location", "allow", "vary", "x-frame-options",
                   "x-content-type-options", "referrer-policy", "cross-origin-opener-policy")
RUST_BIN = ROOT / "rust/target/release/black-rod"


@dataclass(frozen=True)
class Case:
    path: str
    method: str = "GET"
    accept: str = "application/json"
    body: str | None = None
    authorization: str | None = None
    content_type: str = "application/json"


@dataclass
class Server:
    name: str
    port: int
    process: subprocess.Popen

    def connect(self):
        return HTTPConnection(HOST, self.port, timeout=30)


def available_port():
    with socket.socket() as sock:
        sock.bind((HOST, 0))
        return sock.getsockname()[1]


@contextmanager
def start_server(name, command, port, env):
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(command, cwd=ROOT, env={**os.environ, **env},
                                   stdout=log, stderr=log)
        server = Server(name, port, process)
        try:
            for _ in range(200):
                if process.poll() is not None:
                    raise RuntimeError(f"{name} exited with status {process.returncode}")
                try:
                    status, _, _ = request(server, Case("/robots.txt"))
                    if status != 200:
                        raise RuntimeError(f"{name} readiness check returned {status}")
                    break
                except ConnectionError:
                    sleep(0.05)
            else:
                raise TimeoutError(f"{name} failed to start")
            yield server
        except BaseException:
            log.seek(0, 2)
            log.seek(max(0, log.tell() - 6000))
            print(f"{name} log\n{log.read().decode(errors='replace')}")
            raise
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


@contextmanager
def servers(database, cold=False):
    django_port = available_port()
    rust_port = available_port()
    while rust_port == django_port:
        rust_port = available_port()
    sqlite_mode = "rw" if os.environ.get("BLACK_ROD_PARITY_WRITABLE") == "1" else "ro"
    database_url = os.environ.get("BLACK_ROD_PARITY_POSTGRES_URL", f"{database.as_uri().replace('file:', 'sqlite:', 1)}?mode={sqlite_mode}")
    django_env = {
        "PYTHONPATH": f"{Path(__file__).resolve().parent}{os.pathsep}{os.environ['PYTHONPATH']}",
        "BLACK_ROD_PARITY_CACHE_MODE": "cold" if cold else "warm",
        "BLACK_ROD_PARITY_MEDIA_ROOT": str(Path(os.environ["BR_STATE"]) / "media"),
        "SECRET_KEY": "disposable-local-parity-key",
        "LIVE_MITTAB_DIRECTORY_URL": os.environ.get("LIVE_MITTAB_DIRECTORY_URL", "http://127.0.0.1:1"),
        "MITTAB_PRIVATE_API_TOKENS": "fixture-one, fixture-two",
        "FORUM_PROFILE_API_TOKEN": "fixture-forum",
        "ROUND_AMENDMENTS_FILE": str(Path(os.environ["BR_STATE"]) / "round-amendments.json"),
    }
    rust_env = {
        "BLACK_ROD_DATABASE_URL": database_url,
        "LIVE_MITTAB_DIRECTORY_URL": django_env["LIVE_MITTAB_DIRECTORY_URL"],
        "BLACK_ROD_BIND": f"{HOST}:{rust_port}",
        "BLACK_ROD_CACHE_TTL_SECONDS": "0" if cold else "300",
        "TOKIO_WORKER_THREADS": "8",
        "BLACK_ROD_MEDIA_ROOT": str(Path(os.environ["BR_STATE"]) / "media"),
        "ROUND_AMENDMENTS_FILE": django_env["ROUND_AMENDMENTS_FILE"],
        "SECRET_KEY": django_env["SECRET_KEY"],
        "MITTAB_PRIVATE_API_TOKENS": django_env["MITTAB_PRIVATE_API_TOKENS"],
        "FORUM_PROFILE_API_TOKEN": django_env["FORUM_PROFILE_API_TOKEN"],
    }
    if os.environ.get("BLACK_ROD_PARITY_PRODUCTION_ERRORS") == "1":
        rust_env["DEBUG"] = "0"
    memcached = os.environ.get("BLACK_ROD_PARITY_MEMCACHED_ADDR")
    if memcached and not cold:
        host, _, port = memcached.rpartition(":")
        if host != HOST or not port.isdecimal():
            raise ValueError("The Rust comparison only connects to local Memcached")
        rust_env["BLACK_ROD_MEMCACHED_ADDR"] = memcached
    gunicorn = [str(ROOT / "venv/bin/gunicorn"), "--bind", f"{HOST}:{django_port}",
                "--workers", "1", "--threads", "8", "reference:application"]
    with start_server("Django", gunicorn, django_port, django_env) as django:
        with start_server("Rust", [str(RUST_BIN)], rust_port, rust_env) as rust:
            yield (django, rust)


def request(server, case, connection=None):
    owns_connection = connection is None
    if owns_connection:
        connection = server.connect()
    try:
        headers = {"Host": HOST, "Accept": case.accept}
        if case.body is not None:
            headers["Content-Type"] = case.content_type
        if case.authorization is not None:
            headers["Authorization"] = case.authorization
        connection.request(case.method, case.path, body=case.body, headers=headers)
        response = connection.getresponse()
        headers = {key: response.getheader(key) for key in TRACKED_HEADERS
                   if response.getheader(key) is not None}
        return response.status, headers, response.read()
    finally:
        if owns_connection:
            connection.close()


def differences(reference, candidate, position="$"):
    if isinstance(reference, dict) and isinstance(candidate, dict):
        if reference.keys() != candidate.keys():
            yield f"{position} keys differ: {reference.keys() ^ candidate.keys()}"
        for key in reference.keys() & candidate.keys():
            yield from differences(reference[key], candidate[key], f"{position}.{key}")
    elif isinstance(reference, list) and isinstance(candidate, list):
        if position == "$.videos":
            reference = sorted(reference, key=lambda row: row["id"])
            candidate = sorted(candidate, key=lambda row: row["id"])
        elif position.endswith(".tags") and all(isinstance(item, str) for item in reference + candidate):
            reference = sorted(reference)
            candidate = sorted(candidate)
        elif position.endswith(".coty_breakdown") or (
            ".standings.coty[" in position and position.endswith(".breakdown")
        ):
            # Django orders the full breakdown by points and leaves equal
            # points unordered. Compare each group's exact membership and data.
            def order_ties(rows):
                return [row for _, tied in groupby(rows, key=lambda row: row["points"])
                        for row in sorted(tied, key=lambda row: row["debater"]["id"])]

            reference = order_ties(reference)
            candidate = order_ties(candidate)
        if len(reference) != len(candidate):
            yield f"{position} length differs: {len(reference)} vs {len(candidate)}"
        for index, (left, right) in enumerate(zip(reference, candidate)):
            yield from differences(left, right, f"{position}[{index}]")
    elif reference != candidate or (isinstance(reference, bool) != isinstance(candidate, bool)):
        yield f"{position}: {str(reference)[:140]!r} vs {str(candidate)[:140]!r}"


class ProxyContent(HTMLParser):
    def __init__(self, response):
        super().__init__()
        self.data = {}
        self.tag = None
        self.feed(response.decode())

    def handle_starttag(self, tag, _):
        if tag in ("title", "h1", "p", "pre"):
            self.tag = tag

    def handle_data(self, text):
        if self.tag is not None:
            self.data[self.tag] = self.data.get(self.tag, "") + text

    def handle_endtag(self, tag):
        if tag == self.tag:
            self.tag = None

    def payload(self):
        if "pre" in self.data:
            self.data["pre"] = json.loads(self.data["pre"])
        return self.data


def compare(django, rust, cases):
    failures = []
    for case in cases:
        reference = request(django, case)
        candidate = request(rust, case)
        label = f"{case.method} {case.path}"
        if reference[:2] != candidate[:2]:
            failures.append(f"{label}\n  HTTP status/headers {reference[:2]} vs {candidate[:2]}")
        if reference[2] == candidate[2]:
            continue
        content_type = reference[1].get("content-type", "")
        if content_type == candidate[1].get("content-type") and "json" in content_type:
            diff = differences(json.loads(reference[2]), json.loads(candidate[2]))
        elif case.path.startswith("/llm/") and content_type == candidate[1].get("content-type") == "text/html; charset=utf-8":
            diff = differences(ProxyContent(reference[2]).payload(), ProxyContent(candidate[2]).payload())
        else:
            diff = [f"Body differs ({len(reference[2])} vs {len(candidate[2])} bytes)"]
        delta = list(diff)
        if delta:
            failures.append(f"{label}\n  " + "\n  ".join(delta[:10]))
    if failures:
        raise AssertionError(f"{len(failures)} HTTP differences\n" + "\n".join(failures))
    print(f"Matched {len(cases)} HTTP cases")


def fixture_cases(django):
    active = json.loads(request(django, Case("/api/schools/"))[2])
    school = active["schools"][0]["id"]
    cases = [
        Case("/api/schools/"), Case("/api/schools/all/"),
        Case(f"/api/debaters/{school}/"), Case(f"/api/debaters/00{school}/"),
        Case("/api/debaters/9223372036854775807/"),
        Case("/api/schools?keep=a%20b&keep=c"),
        Case("/api/debaters/7/detail?limit=2"),
        Case("/core/my-profile?source=legacy"),
        Case("/core/claim-debater/request"),
        Case("/core/tournaments/import?next=results%20export"),
        Case("/core/debaters/partnership-paths?season=2025"),
        Case("/api/schools/all"), Case(f"/api/debaters/{school}?q=t"),
        Case("/api/schedule"), Case("/api/standings"), Case("/api/oty-guide"), Case("/llm/oty-guide"),
        Case("/api/schools/", "POST"), Case("/api/schools/", "PUT"),
        Case("/api/schools/", accept="application/xml"),
        Case("/api/schools/?format=bogus"), Case("/api/schools/?format=json"),
        Case("/api/schools/?format=json", accept="text/html"),
        Case("/api/schools/?format=json", accept="text/*"),
        Case("/api/schools/?format=json", accept="*/*"),
        Case("/api/schools/?format=json", accept="text/html, application/json"),
        Case("/api/schools/?format=api", accept="application/json"),
        Case("/api/schools/", accept="application/json; indent=4"),
        Case("/api/schools/", accept="Application/JSON"),
        Case("/api/schools/", accept="APPLICATION/*"),
        Case("/api/schools/", accept='Application/JSON; INDENT="4"'),
        Case("/api/schools/", "HEAD"), Case("/api/schools/", "OPTIONS"),
        Case("/api/schools/all/", "OPTIONS"), Case(f"/api/debaters/{school}/", "OPTIONS"),
        Case("/api/schedule/", "OPTIONS"), Case("/api/standings/", "OPTIONS"),
        Case("/api/oty-guide/", "OPTIONS"),
        Case("/robots.txt"), Case("/robots.txt", "HEAD"), Case("/llms.txt"),
        Case("/core/videos/tags/Caf%C3%A9%20Debate"),
        Case("/core/videos/tags/Tagged%20%22Debate%22"),
        Case("/.well-known/ai-plugin.json"),
        Case("/.well-known/openapi.json", accept="application/vnd.oai.openapi+json"),
        Case("/.well-known/openapi.json", accept="APPLICATION/VND.OAI.OPENAPI+JSON"),
        Case("/.well-known/openapi.json", accept="application/*"),
        Case("/.well-known/openapi.json"),
        Case("/.well-known/openapi.json", accept="application/xml"),
        Case("/.well-known/openapi.json?format=json", accept="*/*"),
        Case("/.well-known/openapi.json?format=xml", accept="*/*"),
        Case("/.well-known/openapi.json", "OPTIONS", "*/*"),
        Case("/.well-known/openapi.json", "OPTIONS", "text/html"),
        Case("/.well-known/openapi.json", "HEAD", "*/*"),
        Case("/.well-known/openapi.json?format=xml", "HEAD", "*/*"),
        Case("/.well-known/openapi.json", "POST", "*/*"),
        Case("/.well-known/openapi.json", "DELETE", "*/*"),
        Case("/llm/"),
        Case("/llm/?endpoint=/api/schools/"),
        Case("/llm/?endpoint=/api/standings/%3Fseason%3D2025%26limit%3D2"),
        Case("/llm/?endpoint=/.well-known/openapi.json"),
        Case("/llm/?endpoint=/missing"),
        Case("/llm/?endpoint=/api/unknown/"),
        Case("/llm/?endpoint=/api/agent/v1/"),
        Case("/llm/?endpoint=/api/agent/v1/batches/123/undo/"),
        Case("/llm/?endpoint=/api/agent/v1/batches/unknown/"),
        Case("/llm/?endpoint=/api/private/forum-profiles/"),
        Case("/llm/?endpoint=/api/private/debater-emails/", authorization="Bearer fixture-one"),
        Case(f"/api/schools/{school}/detail/?season=2025&limit=3"),
        Case(f"/api/schools/{school}/detail/?season=2007"),
        Case(f"/api/schools/{school}/detail/", "OPTIONS"),
        Case("/api/schools/9223372036854775807/detail/"),
        Case("/llm/oty-guide/"), Case("/api/oty-guide/"), Case("/api/oty-guide/?scope=example"),
        Case("/api/schedule/?season=never-matched"), Case("/api/schedule/?season=02025"),
        Case("/api/schedule/?season=2023&season=2025"),
        Case("/api/standings/?board=not_a_board&limit=2"),
        Case("/api/standings/?season=2025&board=coty&limit=%2B1_0"),
        Case("/api/standings/?season=2025&board=coty&limit=%D9%A2"),
        Case("/api/standings/?season=2025&board=coty&limit=9999999999999999999999"),
        Case("/api/standings/?season=2025&board=coty&limit=1&limit=1__0"),
        Case("/api/standings/?season=2025&board=COTY,soty&board=toty&limit=5"),
        Case("/api/standings/?season=2021&board=online_quals&limit=9"),
        Case("/api/standings/?season=2021&limit=10"),
        Case("/api/standings/?season=2025&limit=10"),
        Case("/api/standings/?season=2020&board=noty,online_quals&limit=7"),
        Case("/api/standings/?season=2024&limit=99999999"),
        Case("/api/standings/?season=2024&limit=bad"),
        Case("/api/standings/?season=2024&limit=-1"),
        Case("/api/standings/?season=2024&limit=4&limit=1"),
    ]
    cases.extend(Case(path) for path in [
        "/core/schools/autocomplete?q=har",
        "/core/schools/autocomplete?q=7",
        "/core/schools/autocomplete/claim?q=har",
        "/core/schools/autocomplete?forward=%5Bbad",
        "/core/teams/autocomplete?q=Harv&page=2",
        "/core/debaters/autocomplete?q=Ann",
        f"/core/debaters/autocomplete?forward=%7B%22school%22%3A{school}%7D",
        "/core/debater-alias-groups/autocomplete?q=Harv",
        "/core/tournaments/autocomplete?q=Harvard",
        "/core/tournaments/all_autocomplete?q=Harvard",
        "/core/videos/tags_no_create?q=Eco",
        "/core/videos/tags?q=Eco",
        "/core/motions/topics_no_create?q=deb",
        "/core/resources/tags_no_create?q=nov",
        "/core/resources/tags?q=zzznonexistent",
        "/core/debaters/partnership-paths/autocomplete/",
        "/core/debaters/partnership-paths/autocomplete/?q=Ann",
        "/core/debaters/partnership-paths/autocomplete/?q=ann&page=2",
        "/core/debaters/partnership-paths/autocomplete/?q=ann+tao",
        "/core/debaters/partnership-paths/autocomplete/?q=ann%25",
        "/core/debaters/partnership-paths/autocomplete/?q=ann_",
        "/core/debaters/partnership-paths/network/data/",
        "/core/debaters/partnership-paths/network/data/?from_season=2019&to_season=2025&qualified=on&min_tournaments=3",
        f"/core/debaters/partnership-paths/network/data/?schools={school}&x_axis=school&y_axis=toty_points",
        "/core/debaters/partnership-paths/network/data/?from_season=2025&to_season=2019&min_tournaments=-1",
        "/core/debaters/partnership-paths/network/paths/?first_debater=5270&second_debater=6015",
        "/core/debaters/partnership-paths/network/paths/?first_debater=5270&second_debater=5270",
        "/core/motions/topics?q=zzznonexistent",
    ])
    cases.extend([
        Case("/api/schools/%37/detail/"),
        Case("/api/schools/%37/detail/?format=invalid"),
        Case("/api/private/debater-emails/", "POST", body=json.dumps({"debater_ids": ["١", "２"]}),
             authorization="Bearer fixture-one"),
        Case("/api/private/debater-emails/", "POST", body='{"debater_ids":[1]}'),
        Case("/api/private/debater-emails/", "POST", body='{"debater_ids":[1,2,"1",999999]}',
             authorization="Bearer fixture-two"),
        Case("/api/private/debater-emails/", "POST", body='{"debater_ids":[null]}', authorization="Bearer fixture-one"),
        Case("/api/private/debater-emails/", "POST", body='{"debater_ids":"1"}', authorization="Bearer fixture-one"),
        Case("/api/private/forum-profiles/", "POST", body='{"wordpress_user_id":1}'),
        Case("/api/private/forum-profiles/", "POST", body='{"wordpress_user_id":9000001}',
             authorization="Bearer fixture-forum"),
        Case("/api/private/forum-profiles/", "POST", body='{"wordpress_user_id":true}',
             authorization="Bearer fixture-forum"),
        Case("/api/private/forum-profiles/", "OPTIONS"),
        Case("/api/private/debater-emails/", "OPTIONS"),
        Case("/api/private/forum-profiles/", accept="application/xml"),
        Case("/api/private/forum-profiles/?format=nope"),
        Case("/api/private/forum-profiles/", "POST", body="wordpress_user_id=1",
             authorization="Bearer fixture-forum", content_type="application/x-www-form-urlencoded"),
        Case("/api/private/debater-emails/", "POST", body="debater_ids=1",
             authorization="Bearer fixture-one", content_type="application/x-www-form-urlencoded"),
        Case("/api/private/forum-profiles/", "POST", body="other=value",
             authorization="Bearer fixture-forum", content_type="text/plain"),
        Case("/api/agent/v1/", accept="application/xml"),
        Case("/api/agent/v1/?format=nope"),
        Case("/api/standings/replay/", "OPTIONS"),
        Case("/api/standings/through-date/", "OPTIONS"),
        Case("/api/standings/through-date/"),
        Case("/api/standings/through-date/?through=bad"),
        Case("/api/standings/through-date/?season=2025&date=20251001"),
        Case("/api/standings/through-date/?season=2025&through=2025-W41"),
        Case("/api/standings/through-date/?season=2025&through=2025-09-01"),
        Case("/api/standings/through-date/?season=2025&through=2026-01-01"),
        Case("/api/standings/through-date/?season=2025&through=2026-05-01&board=coty&limit=8"),
        Case("/api/standings/replay/?season=2025&board=soty,coty&limit=3"),
    ])
    for season in range(2004, 2027):
        cases.append(Case(f"/api/schedule/?season={season}"))
        cases.append(Case(f"/api/standings/?season={season}"))
        cases.append(Case(f"/api/standings/replay/?season={season}"))
    standings = json.loads(request(django, Case("/api/standings/?season=2025&board=toty&limit=5"))[2])
    for item in standings["standings"]["toty"]:
        team = item["team"]["id"]
        cases.append(Case(f"/api/teams/{team}/detail/"))
        cases.append(Case(f"/api/teams/{team}/detail/?limit=2"))
    cases.append(Case("/api/teams/9223372036854775807/detail/"))
    tournaments = [marker["tournament"]["id"]
                   for standing in standings["standings"]["toty"] for marker in standing["markers"]]
    for tournament in list(dict.fromkeys(tournaments))[:7]:
        cases.append(Case(f"/api/tournaments/{tournament}/detail/"))
    cases.append(Case("/api/tournaments/9223372036854775807/detail/"))
    debaters = [debater["id"] for standing in standings["standings"]["toty"]
                for debater in standing["team"]["debaters"]]
    for debater in list(dict.fromkeys(debaters))[:7]:
        cases.append(Case(f"/api/debaters/{debater}/detail/"))
        cases.append(Case(f"/api/debaters/{debater}/detail/?limit=2"))
    cases.append(Case("/api/debaters/9223372036854775807/detail/"))
    for item in active["schools"]:
        cases.append(Case(f"/api/debaters/{item['id']}/"))
        cases.append(Case(f"/api/schools/{item['id']}/detail/?season=2025&limit=5"))
    return cases, school


def process_rss(pid):
    rows = subprocess.check_output(["ps", "-axo", "pid=,ppid=,rss="], text=True)
    processes = [tuple(map(int, line.split())) for line in rows.splitlines()]
    descendants = {pid}
    while True:
        found = {child for child, parent, _ in processes if parent in descendants} | descendants
        if found == descendants:
            return sum(rss for child, _, rss in processes if child in descendants)
        descendants = found


def percentile(values, p):
    values = sorted(values)
    return values[math.ceil(len(values) * p) - 1]


def benchmark(server, case, requests, concurrency):
    def worker(count):
        latencies = []
        with closing(server.connect()) as connection:
            for _ in range(count):
                start = perf_counter()
                status, _, body = request(server, case, connection)
                latencies.append((perf_counter() - start) * 1000)
                if status != 200 or not body:
                    raise RuntimeError(f"{server.name} {case.path} returned {status}")
        return latencies

    stop = Event()
    memory = [process_rss(server.process.pid)]

    def sample_memory():
        while not stop.wait(0.10):
            memory.append(process_rss(server.process.pid))

    sampler = Thread(target=sample_memory, daemon=True)
    sampler.start()
    try:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            start = perf_counter()
            runs = executor.map(worker, [
                requests // concurrency + (index < requests % concurrency)
                for index in range(concurrency)
            ])
            latencies = [milliseconds for run in runs for milliseconds in run]
            elapsed = perf_counter() - start
    finally:
        stop.set()
        sampler.join()
    memory.append(process_rss(server.process.pid))
    return {
        "requests": requests,
        "concurrency": concurrency,
        "requests_per_second": round(requests / elapsed, 2),
        "p50_ms": round(percentile(latencies, 0.50), 3),
        "p95_ms": round(percentile(latencies, 0.95), 3),
        "sampled_peak_tree_rss_mib": round(max(memory) / 1024, 2),
    }


def test_database_snapshot(database):
    return hashlib.file_digest(database.open("rb"), "sha256").hexdigest() if hasattr(hashlib, "file_digest") else hashlib.sha256(database.read_bytes()).hexdigest()


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", action="store_true")
    parser.add_argument("--benchmark-path", action="append", metavar="PATH",
                        help="measure the specified GET endpoint in place of the default benchmark set")
    parser.add_argument("--warm-requests", type=int, default=1200)
    parser.add_argument("--cold-requests", type=int, default=30)
    parser.add_argument("--concurrency", type=int, default=12)
    args = parser.parse_args()
    if args.benchmark_path and not args.benchmark:
        parser.error("--benchmark-path requires --benchmark")
    if not (0 < args.concurrency <= args.warm_requests and args.cold_requests > 0):
        parser.error("Request counts must be positive and cover the specified concurrency")
    database = (Path(os.environ["BR_STATE"]) / "dev.sqlite3").resolve(strict=True)
    using_postgres = bool(os.environ.get("BLACK_ROD_PARITY_POSTGRES_URL"))
    before = None if using_postgres else test_database_snapshot(database)
    if not using_postgres:
        with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
            rows = connection.execute("SELECT COUNT(*) FROM core_debater").fetchone()[0]
        assert rows, "Load the local dev fixture first"
    try:
        with servers(database) as (django, rust):
            cases, school = fixture_cases(django)
            compare(django, rust, cases)
        if not args.benchmark:
            return
        endpoint_cases = [
            Case("/api/schools/"), Case("/api/schools/all/"),
            Case(f"/api/debaters/{school}/"), Case("/api/schedule/?season=2025"),
            Case("/api/standings/?season=2025"), Case("/api/standings/replay/?season=2025"),
        ]
        for name in ("teams", "tournaments", "debaters"):
            endpoint_cases.append(next(case for case in cases
                if case.path.startswith(f"/api/{name}/") and case.path.endswith("/detail/")
                and "9223372036854775807" not in case.path))
        if args.benchmark_path:
            endpoint_cases = [Case(path) for path in args.benchmark_path]
        results = []
        for cold in (False, True):
            for case in endpoint_cases:
                with servers(database, cold=cold) as (django, rust):
                    compare(django, rust, [case])
                    result = {"path": case.path, "cache": "disabled" if cold else "warm"}
                    for server in (django, rust):
                        result[server.name] = benchmark(
                            server, case, args.cold_requests if cold else args.warm_requests,
                            1 if cold else args.concurrency,
                        )
                    results.append(result)
        print(json.dumps({"platform": platform.platform(), "results": results}, indent=2))
    finally:
        if before is not None:
            assert test_database_snapshot(database) == before, "Servers changed fixture database bytes"


if __name__ == "__main__":
    main()

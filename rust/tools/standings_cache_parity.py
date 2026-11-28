"""Check cross-runtime standings and search cache invalidation."""
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from unittest.mock import patch

from bs4 import BeautifulSoup
from pymemcache.client.base import Client

from agent_write_parity import fixture_database
from compare import Case, HOST, ROOT, RUST_BIN, available_port, request, servers, start_server

GENERATION = ":1:black-rod:rust:standings-generation:v1"
SEARCH = ":1:black-rod:search-index:generation:v2"
PATH = "/api/standings/?season=2025"

SEED = """
from reference import application
from core.models import COTY, Debater, School
school = School.objects.create(name="Mixed-runtime cache fixture", included_in_oty=True)
COTY.objects.create(school=school, season="2025", points=100, place=1)
person = Debater.objects.create(first_name="Beforecache", last_name="Searchfixture", school=school)
print(school.pk, person.pk)
"""

CHANGE = """
from reference import application
from core.models import COTY, Debater
from core.utils.rankings import redo_rankings
from django.core.cache import cache
from django.core.cache.utils import make_template_fragment_key
from django.db import transaction
from sys import argv

class FixtureRollback(Exception):
    pass

keys = (make_template_fragment_key("coty", ["2025"]), "api:standings_replay:2025",
        "live-standings:v4:2025", "live-standings:v4:2025:stale")
cache.set_many(dict.fromkeys(keys, b"old-standings"), timeout=60)
try:
    with transaction.atomic():
        target = COTY.objects.filter(school_id=int(argv[1]), season="2025")
        target.update(points=200)
        redo_rankings(COTY.objects.filter(season="2025"), season="2025", cache_type="coty")
        person = Debater.objects.get(pk=int(argv[2]))
        person.first_name = "Aftercache"
        person.save(update_fields=["first_name"])
        assert all(cache.get(key) == b"old-standings" for key in keys)
        if argv[3] == "rollback":
            raise FixtureRollback
except FixtureRollback:
    assert all(cache.get(key) == b"old-standings" for key in keys)
else:
    assert all(cache.get(key) is None for key in keys)
"""


def points(server, school):
    status, _, body = request(server, Case(PATH))
    assert status == 200, (server.name, status)
    board = json.loads(body)["standings"]["coty"]
    return next(row["points"] for row in board if row["school"]["id"] == school)


def search_match(server, debater, query):
    status, _, body = request(server, Case(f"/search/?q={query}", accept="text/html"))
    assert status == 200, (server.name, status)
    return BeautifulSoup(body, "html.parser").select_one(f'a[href="/core/debaters/{debater}"]')


RENAME = """
from reference import application
from core.models import Debater
from sys import argv
if argv[3] == "signal":
    person = Debater.objects.get(pk=int(argv[1]))
    person.first_name = argv[2]
    person.save(update_fields=["first_name"])
else:
    Debater.objects.filter(pk=int(argv[1])).update(first_name=argv[2])
"""


def search_runtime(database, port):
    return {
        "BLACK_ROD_BIND": f"{HOST}:{port}", "BLACK_ROD_RELEASE_ROOT": str(database.parent),
        "BLACK_ROD_DATABASE_URL": os.environ.get(
            "BLACK_ROD_PARITY_POSTGRES_URL", f"{database.as_uri().replace('file:', 'sqlite:', 1)}?mode=ro"
        ),
        "DOT_ENV_FILE": os.devnull, "LIVE_MITTAB_DIRECTORY_URL": "",
    }


def check_local_reindex(database, debater, env):
    port = available_port()
    runtime = search_runtime(database, port)
    django = {**env, "BLACK_ROD_RELEASE_ROOT": str(database.parent)}
    django.pop("BLACK_ROD_PARITY_MEMCACHED_ADDR", None)
    django.pop("BLACK_ROD_SEARCH_GENERATION_FILE", None)
    change = [str(ROOT / "venv/bin/python"), "-c", RENAME, str(debater)]
    with patch.dict(os.environ):
        os.environ.pop("BLACK_ROD_MEMCACHED_ADDR", None)
        os.environ.pop("BLACK_ROD_SEARCH_GENERATION_FILE", None)
        with start_server("Rust local search", [str(RUST_BIN)], port, runtime) as rust:
            assert search_match(rust, debater, "Recoveredcache")
            subprocess.run(change + ["Localcache", "signal"], env=django, check=True)
            assert search_match(rust, debater, "Localcache")
            assert not search_match(rust, debater, "Recoveredcache")
            subprocess.run(change + ["Reindexed", "bulk"], env=django, check=True)
            assert not search_match(rust, debater, "Reindexed")
            subprocess.run([str(RUST_BIN), "reindex_haystack"], env={**os.environ, **runtime},
                           check=True, capture_output=True)
            assert search_match(rust, debater, "Reindexed")
            assert not search_match(rust, debater, "Localcache")
            subprocess.run(change + ["Djangoreindex", "bulk"], env=django, check=True)
            assert not search_match(rust, debater, "Djangoreindex")
            subprocess.run([str(ROOT / "venv/bin/python"), "-c", """
from reference import application
from django.core.management import call_command
call_command("reindex_haystack", verbosity=0)
"""], env=django, check=True, capture_output=True)
            assert search_match(rust, debater, "Djangoreindex")


def check_search_recovers_if_generation_is_unchanged(database, debater, env, cache):
    port = available_port()
    runtime = {
        **search_runtime(database, port), "BLACK_ROD_CACHE_TTL_SECONDS": "2",
        "BLACK_ROD_MEMCACHED_ADDR": env["BLACK_ROD_PARITY_MEMCACHED_ADDR"],
    }
    with start_server("Rust bounded search", [str(RUST_BIN)], port, runtime) as rust:
        assert search_match(rust, debater, "Aftercache")
        before = cache.get(SEARCH)
        subprocess.run([str(ROOT / "venv/bin/python"), "-c", RENAME, str(debater), "Recoveredcache", "bulk"],
                       env=env, check=True)
        deadline = monotonic() + 8
        while not search_match(rust, debater, "Recoveredcache"):
            assert monotonic() < deadline, "Search retained an expired index with the old cache generation"
            sleep(0.1)
        assert not search_match(rust, debater, "Aftercache")
        assert cache.get(SEARCH) == before, "The expiry check must keep the shared generation unchanged"


def main():
    seed = Path(os.environ["BR_STATE"]) / "dev.sqlite3"
    postgres = os.environ.get("BLACK_ROD_PARITY_POSTGRES_URL")
    host, _, port = os.environ["BLACK_ROD_PARITY_MEMCACHED_ADDR"].rpartition(":")
    if host != HOST or not port.isdecimal():
        raise ValueError("Standings cache tests require local Memcached")
    with TemporaryDirectory(prefix="standings-cache-parity-") as folder:
        database = Path(folder) / "dev.sqlite3"
        with fixture_database(seed, database, postgres):
            os.environ["BR_STATE"] = folder
            env = {**os.environ, "PYTHONPATH": f"{Path(__file__).resolve().parent}{os.pathsep}{os.environ['PYTHONPATH']}"}
            python = str(ROOT / "venv/bin/python")
            school, debater = map(int, subprocess.check_output(
                [python, "-c", SEED], text=True, env=env
            ).splitlines()[-1].split())
            changes = [python, "-c", CHANGE, str(school), str(debater)]
            with servers(database) as (django, rust), closing(Client((host, int(port)), timeout=2)) as cache:
                assert points(rust, school) == 100
                assert search_match(rust, debater, "Beforecache")
                before = cache.get(GENERATION)
                search_before = cache.get(SEARCH)
                assert len(search_before) == 16
                assert len(before) == 16
                subprocess.run(changes + ["rollback"], check=True, env=env)
                assert cache.get(GENERATION) == before and cache.get(SEARCH) == search_before
                assert points(rust, school) == 100
                assert not search_match(rust, debater, "Aftercache")

                subprocess.run(changes + ["commit"], check=True, env=env)
                after = cache.get(GENERATION)
                assert len(after) == 16 and after != before
                assert points(rust, school) == points(django, school) == 200
                assert cache.get(SEARCH) != search_before
                assert search_match(rust, debater, "Aftercache")
                assert not search_match(rust, debater, "Beforecache")
                check_search_recovers_if_generation_is_unchanged(database, debater, env, cache)
            check_local_reindex(database, debater, env)
    print("Search refreshes after Django edits, local reindexing and a missed shared generation update")


if __name__ == "__main__":
    main()

"""Check that a committed Django ranking update refreshes a warmed Rust API."""
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from bs4 import BeautifulSoup
from pymemcache.client.base import Client

from agent_write_parity import fixture_database
from compare import Case, HOST, ROOT, request, servers

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
from django.db import transaction
from sys import argv

class FixtureRollback(Exception):
    pass

try:
    with transaction.atomic():
        target = COTY.objects.filter(school_id=int(argv[1]), season="2025")
        target.update(points=200)
        redo_rankings(COTY.objects.filter(season="2025"), season="2025", cache_type="coty")
        person = Debater.objects.get(pk=int(argv[2]))
        person.first_name = "Aftercache"
        person.save(update_fields=["first_name"])
        if argv[3] == "rollback":
            raise FixtureRollback
except FixtureRollback:
    pass
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
    print("Committed Django edits refresh warmed Rust standings and search; rollback preserves both generations")


if __name__ == "__main__":
    main()

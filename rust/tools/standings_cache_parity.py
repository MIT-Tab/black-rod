"""Check that a committed Django ranking update refreshes a warmed Rust API."""
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from pymemcache.client.base import Client

from agent_write_parity import fixture_database
from compare import Case, HOST, ROOT, request, servers

GENERATION = ":1:black-rod:rust:standings-generation:v1"
PATH = "/api/standings/?season=2025"

SEED = """
from reference import application
from core.models import COTY, School
school = School.objects.create(name="Mixed-runtime cache fixture", included_in_oty=True)
COTY.objects.create(school=school, season="2025", points=100, place=1)
print(school.pk)
"""

CHANGE = """
from reference import application
from core.models import COTY
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
        if argv[2] == "rollback":
            raise FixtureRollback
except FixtureRollback:
    pass
"""


def points(server, school):
    status, _, body = request(server, Case(PATH))
    assert status == 200, (server.name, status)
    board = json.loads(body)["standings"]["coty"]
    return next(row["points"] for row in board if row["school"]["id"] == school)


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
            school = int(subprocess.check_output([python, "-c", SEED], text=True, env=env).splitlines()[-1])
            with servers(database) as (django, rust), closing(Client((host, int(port)), timeout=2)) as cache:
                assert points(rust, school) == 100
                before = cache.get(GENERATION)
                assert len(before) == 16
                subprocess.run([python, "-c", CHANGE, str(school), "rollback"], check=True, env=env)
                assert cache.get(GENERATION) == before
                assert points(rust, school) == 100

                subprocess.run([python, "-c", CHANGE, str(school), "commit"], check=True, env=env)
                after = cache.get(GENERATION)
                assert len(after) == 16 and after != before
                assert points(rust, school) == points(django, school) == 200
    print("A committed Django ranking update refreshes warmed Rust standings; rollback preserves the generation")


if __name__ == "__main__":
    main()

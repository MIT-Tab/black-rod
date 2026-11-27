"""Check current-season results saves on cloned PostgreSQL fixtures."""

from argparse import ArgumentParser
from contextlib import closing
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
from statistics import median
import subprocess
from tempfile import TemporaryDirectory
from time import perf_counter
from urllib.parse import urlencode

import psycopg2

import compare
from agent_write_parity import fixture_database

SEED = """
from reference import application
from datetime import date
from django.contrib.sessions.backends.db import SessionStore
from core.models import Debater, QUAL, School, Team, TeamResult, Tournament, TournamentAutoQual, User
import json

host = School.objects.create(name="Large import fixture")
event = Tournament.objects.create(host=host, season="2026", date=date(2026, 10, 14),
    manual_name="Large import benchmark save")
regular = Tournament.objects.bulk_create([
    Tournament(name=f'Large import points {i}', short_name=f'Large {i}', host=host, season="2026",
        date=date(2026, 10, i + 1), num_teams=64, qual=True, toty=True, online_qual_points=True)
    for i in range(10)])
auto = Tournament.objects.create(host=host, season="2026", date=date(2026, 10, 18), qual_type=Tournament.YALE)
people = Debater.objects.bulk_create([Debater(first_name="Batch", last_name=str(i), school=host,
    first_season="2025", latest_season="2025") for i in range(620)])
teams = Team.objects.bulk_create([Team(name=f'Bulk team {i}', short_name=f'Bulk {i}') for i in range(310)])
Team.debaters.through.objects.bulk_create([Team.debaters.through(team=teams[i//2], debater=person)
    for i, person in enumerate(people)])
TeamResult.objects.bulk_create([TeamResult(tournament=regular[i // 31], team=team, place=i % 31 + 1)
    for i, team in enumerate(teams)])
TournamentAutoQual.objects.bulk_create([TournamentAutoQual(tournament=auto, debater=person)
    for person in people[:550]])
standalone = Debater.objects.create(first_name="Standalone", last_name="Qualification", school=host)
QUAL.objects.create(season="2026", debater=standalone, qual_type=QUAL.WORLDS, place=7,
    points=8, tied=True, tournament=auto)
user = User.objects.create_superuser(username="batch-import-test")
session = SessionStore()
session.update({"_auth_user_id": str(user.pk), "_auth_user_backend": "django.contrib.auth.backends.ModelBackend",
                "_auth_user_hash":user.get_session_auth_hash()})
session.save()
print(json.dumps((event.id, session.session_key)))
"""

def request_save(server, session, event):
    with closing(server.connect()) as client:
        path = f"/core/tournaments/data_entry?tournament={event}"
        client.request("GET", path, headers={"Cookie": f"sessionid={session}"})
        response = client.getresponse()
        assert response.status == 200, (server.name, response.status)
        cookie = SimpleCookie()
        for name, value in response.getheaders():
            if name.lower() == "set-cookie":
                cookie.load(value)
        response.read()
        csrf = cookie["csrftoken"].value
        data = {}
        for tab in ("varsity_teams", "novice_teams", "varsity_speakers", "novice_speakers", "unplaced_teams"):
            data[f"{tab}-INITIAL_FORMS"] = 0
            data[f"{tab}-TOTAL_FORMS"] = 0
        data["csrfmiddlewaretoken"] = csrf
        start = perf_counter()
        client.request("POST", path, urlencode(data), {"Cookie": f"sessionid={session}; csrftoken={csrf}",
            "Content-Type": "application/x-www-form-urlencoded"})
        response = client.getresponse()
        assert response.status == 302, (server.name, response.status)
        response.read()
        return (perf_counter() - start) * 1000

def snapshot(conn):
    contents = []
    for table in ("core_qual", "core_qualpoints", "core_onlinequal", "core_coty"):
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT * FROM {table} WHERE season=%s", ("2026",))
            idx = [entry.name for entry in cursor.description].index("id")
            results = sorted([tuple(row[:idx] + row[idx+1:]) for row in cursor.fetchall()], key=repr)
            contents.append(results)
    with conn.cursor() as cursor:
        cursor.execute("SELECT id,latest_season FROM core_debater WHERE first_name='Batch' ORDER BY id")
        contents.append(cursor.fetchall())
    return contents

def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--prior-rust", type=Path, help="Optional earlier release binary to benchmark.")
    args = parser.parse_args()
    if args.samples < 1:
        parser.error("--samples must be positive")
    postgres = os.environ.get("BLACK_ROD_PARITY_POSTGRES_URL")
    if not postgres:
        parser.error("A local BLACK_ROD_PARITY_POSTGRES_URL fixture is required.")
    seed = Path(os.environ["BR_STATE"]) / "dev.sqlite3"
    default_binary = compare.RUST_BIN
    runtimes = {"Django": default_binary, "Rust": default_binary}
    if args.prior_rust:
        runtimes["Prior Rust"] = args.prior_rust.resolve(strict=True)
    documents = {}
    with TemporaryDirectory(prefix="standings-batch-import-") as folder:
        os.environ.update(BR_STATE=folder, BLACK_ROD_PARITY_WRITABLE="1")
        with fixture_database(seed, seed, postgres):
            env = {
                **os.environ,
                "PYTHONPATH": str(Path(__file__).resolve().parent) + os.pathsep + os.environ["PYTHONPATH"],
                "SECRET_KEY": "disposable-local-parity-key",
            }
            raw = subprocess.check_output([str(compare.ROOT / "venv/bin/python"), "-c", SEED], text=True, env=env)
            event, session = json.loads(raw.splitlines()[-1])
            benchmark_source = os.environ["BLACK_ROD_PARITY_POSTGRES_URL"]
            for label, binary in runtimes.items():
                compare.RUST_BIN = binary
                with fixture_database(seed, seed, benchmark_source):
                    with closing(psycopg2.connect(os.environ["BLACK_ROD_PARITY_POSTGRES_URL"])) as db:
                        with compare.servers(seed) as (django, rust):
                            timings = []
                            for _ in range(args.samples + 1):
                                with db.cursor() as cursor:
                                    cursor.execute("UPDATE core_debater SET latest_season='2025' WHERE first_name='Batch'")
                                db.commit()
                                timings.append(request_save(django if label == "Django" else rust, session, event))
                            documents[label] = snapshot(db)
                            print(label, "median ms", round(median(timings[1:]), 2), flush=True)
    for label, contents in documents.items():
        for name, left, right in zip(("qual", "qualpoints", "onlinequal", "coty", "latest_season"),
                                     documents["Django"], contents):
            assert left == right, (label, name, len(left), len(right))
    print("Current-season result saves produced equivalent qualification and participation data")


if __name__ == "__main__":
    main()

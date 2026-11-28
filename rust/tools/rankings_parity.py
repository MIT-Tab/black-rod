"""Compare administrator recomputation against Django on identical disposable databases."""
from contextlib import closing
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
from urllib.parse import urlencode

from compare import Case, ROOT, request, servers

PATH = "/core/rankings-recompute/"

SEED = """
from django import setup
setup()
from datetime import date
from django.contrib.sessions.backends.db import SessionStore
from core.models import (
    COTY, NOTY, QUAL, SOTY, TOTY, Debater, OnlineQUAL, QualPoints, Round,
    School, SpeakerResult, Team, TeamResult, Tournament, TournamentAutoQual, User,
)
user = User.objects.create_superuser(username="rankings-parity")
session = SessionStore()
session.update({"_auth_user_id": str(user.pk), "_auth_user_backend": "django.contrib.auth.backends.ModelBackend",
                "_auth_user_hash":user.get_session_auth_hash()})
session.save()

school = School.objects.create(name="Ranking primary school")
other = School.objects.create(name="Ranking second school")
excluded = School.objects.create(name="Ranking excluded school", included_in_oty=False)
people = [Debater.objects.create(first_name=f"Person {i}", school=affiliation, first_season="2018", latest_season="2019")
          for i, affiliation in enumerate([school,school,school,school,other,excluded,school,school,school])]
teams = [Team.objects.create(name=f"Partnership {i}") for i in range(6)]
teams[0].debaters.add(*people[:2])
teams[1].debaters.add(*people[2:4])
teams[2].debaters.add(*people[4:6])
teams[3].debaters.add(people[6])
teams[4].debaters.add(people[7])
# Last debater has no results and no rounds. The last team has no results or rounds.
teams[5].debaters.add(people[8])
for year in [2019, 2020, 2026]:
    for i, size in enumerate([32, 40, 15, 32, 80, 50, 40]):
        event = Tournament.objects.create(season=str(year), date=date(year,9,i+1), host=school,
            num_teams=size, num_novice_debaters=48, qual_type=Tournament.YALE if i==0 else Tournament.POINTS,
            autoqual_bar=1 if i==0 else 0, online_qual_points=i<4)
        for place, team in enumerate(teams[:3], 1):
            TeamResult.objects.create(tournament=event, team=team, place=place,
                ghost_points=place == 3, counts_for_points=i!=2)
        for place, person in enumerate(people[:6], 1):
            SpeakerResult.objects.create(tournament=event, debater=person, place=place, tie=place==3,
                counts_for_points=i!=3)
            if year < 2026:
                SpeakerResult.objects.create(tournament=event, debater=person,
                    type_of_place=Debater.NOVICE, place=place)
        if i==0:
            TournamentAutoQual.objects.create(tournament=event, debater=people[6])
            Round.objects.create(tournament=event, gov=teams[3], opp=teams[4], victor=Round.GOV)
    COTY.objects.update_or_create(season=str(year), school=other, defaults={"points":19})
    QualPoints.objects.create(season=str(year), debater=people[6], points=8)
    QualPoints.objects.create(season=str(year), debater=people[5], points=18)
    QUAL.objects.get_or_create(season=str(year), debater=people[7], qual_type=QUAL.WORLDS)
    OnlineQUAL.objects.update_or_create(season=str(year), debater=people[7], defaults={"points":5})
    TOTY.objects.create(season=str(year),team=teams[2],points=20)
    SOTY.objects.update_or_create(season=str(year), debater=people[5], defaults={"points":13})
    NOTY.objects.update_or_create(season=str(year), debater=people[5], defaults={"points":13})
event = Tournament.objects.create(
    season="2026", date=date(2026, 9, 8), host=school, num_teams=40,
)
TeamResult.objects.create(tournament=event, team=teams[0], place=0)
print(session.session_key)
"""


def post(server, headers, data):
    with closing(server.connect()) as client:
        client.request("POST", PATH, urlencode(data), headers)
        response = client.getresponse()
        assert response.status == 200, (server.name, response.status, response.read()[:500])
        return json.loads(response.read())


def exercise(server, session):
    with closing(server.connect()) as client:
        client.request("GET", PATH, headers={"Cookie": f"sessionid={session}"})
        response = client.getresponse()
        assert response.status == 200, (server.name, response.status)
        content = response.read()
        assert b"Rankings Recompute Tool" in content
        cookie = SimpleCookie()
        for name, value in response.getheaders():
            if name.lower() == "set-cookie":
                cookie.load(value)
    csrf = cookie["csrftoken"].value
    headers = {"Cookie": f"sessionid={session}; csrftoken={csrf}",
               "Content-Type": "application/x-www-form-urlencoded",
               "X-CSRFToken": csrf}
    assert post(server, headers, {}) == {"success": False, "error": "Season and ranking type are required"}
    assert post(server, headers, {"season": "2026", "ranking_type": "unknown"}) == {
        "success": False, "error": "Unknown ranking type",
    }
    for ranking, years in [("coty", (2019, 2020, 2026)), ("noty", (2019, 2026)),
                           ("soty", (2019, 2026)), ("toty", (2019, 2026))]:
        for year in years:
            data = post(server, headers, {"season": year, "ranking_type": ranking})
            assert data == {"success": True,
                            "message": f"Successfully recomputed {ranking.upper()} rankings for season {year}"}, (
                server.name, ranking, year, data,
            )


    status, _, body = request(server, Case("/api/standings/replay/?season=2026&board=toty"))
    assert status == 200, (server.name, status)
    rows = json.loads(body)["standings"]["toty"]
    place_zero = [marker["points"] for row in rows for marker in row["all_markers"] if marker["place"] == 0]
    assert place_zero == [5.25], (server.name, place_zero)


def snapshot(path):
    tables = ("core_toty", "core_soty", "core_noty", "core_coty", "core_qual", "core_qualpoints",
              "core_onlinequal", "core_debater", "core_team", "core_team_debaters",
              "core_round", "core_speakerresult", "core_teamresult")
    with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as db:
        def records(table):
            fields = [info[1] for info in db.execute(f"PRAGMA table_info({table})")]
            if table in tables[:7]:
                fields.remove("id")
            rows = db.execute(f"SELECT {','.join(fields)} FROM {table}")
            normalized = (tuple(round(value, 8) if isinstance(value, float) else value for value in row)
                          for row in rows)
            return sorted(normalized, key=repr)

        return {table: records(table) for table in tables}


def main():
    # Clone only the schema and fixture seeded in a directory owned by this run.
    # Never use the shared PostgreSQL comparison URL for administrative writes.
    if os.environ.get("BLACK_ROD_PARITY_POSTGRES_URL"):
        raise ValueError("Ranking write parity uses its own SQLite databases")
    with TemporaryDirectory(prefix="black-rod-rankings-") as directory:
        env = {**os.environ, "BR_STATE": directory, "SECRET_KEY": "disposable-local-parity-key"}
        subprocess.run([str(ROOT / "venv/bin/python"), "manage.py", "migrate", "--noinput", "--verbosity", "0"],
                       env=env, check=True, capture_output=True)
        output = subprocess.check_output([str(ROOT / "venv/bin/python"), "-c", SEED], env=env, text=True)
        session = output.splitlines()[-1]
        original = Path(directory) / "dev.sqlite3"
        variants = [Path(directory) / f"{name}.sqlite3" for name in ("django", "rust")]
        for path in variants:
            with sqlite3.connect(f"{original.as_uri()}?mode=ro", uri=True) as source, sqlite3.connect(path) as copy:
                source.backup(copy)
        os.environ.update({"BR_STATE": directory, "BLACK_ROD_PARITY_WRITABLE": "1"})
        for index, path in enumerate(variants):
            # Django's local settings use BR_STATE/dev.sqlite3. Replace that
            # file only while no fixture services are running.
            path.replace(original)
            with servers(original) as app:
                exercise(app[index], session)
            original.replace(path)
        expected, observed = map(snapshot, variants)
        for table in expected:
            left, right = expected[table], observed[table]
            assert left == right, (table, len(left), len(right),
                                   next(((a,b) for a,b in zip(left,right) if a!=b), None))
        print("Administrator rankings match Django across nine recomputations")


if __name__ == "__main__":
    main()

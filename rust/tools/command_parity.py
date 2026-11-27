"""Compare maintenance commands on separate copies of the local Django fixture."""
from collections import Counter
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory

from compare import ROOT, RUST_BIN, differences


def run(command, database, engine, *arguments):
    env = {**os.environ, "BR_STATE": str(database.parent)}
    if engine == "django":
        cmd = [str(ROOT / "venv/bin/python"), "manage.py", command]
    else:
        env["BLACK_ROD_DATABASE_URL"] = f"sqlite://{database}?mode=rw"
        cmd = [str(RUST_BIN), command]
    result = subprocess.run(cmd + list(arguments), env=env, cwd=ROOT,
                            text=True, capture_output=True, check=True)
    return "\n".join(line for line in result.stdout.splitlines()
                     if line != "Running in development mode")


def snapshot(path, table, columns):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(f"SELECT {columns} FROM {table} ORDER BY id").fetchall()


def verify_late_commands(databases):
    for args in (("--dry-run",), ()):
        output = [run("shorten_names", path, engine, *args) for engine, path in databases.items()]
        assert output[0] == output[1], "Short-name command output differs"
    for table, fields in (
        ("core_tournament", "id,name,short_name,toty,soty,qual,noty,online_qual_points,autoqual_bar"),
        ("core_team", "id,name,short_name"),
    ):
        left, right = [snapshot(path, table, fields) for path in databases.values()]
        assert left == right, f"Short-name command changed {table} differently"

    for path in databases.values():
        with sqlite3.connect(path) as connection:
            connection.execute("INSERT INTO core_qualpoints (points,season,debater_id) VALUES (9,'2026',5270)")
    output = [run("redo_rankings", path, engine).splitlines() for engine, path in databases.items()]
    assert output[0] and Counter(output[0]) == Counter(output[1]), "Different teams recomputed"
    for table, fields in (
        ("core_qualpoints", "season,debater_id,points"),
        ("core_qual", "season,debater_id,qual_type,place,points,tournament_id"),
        ("core_coty", "season,school_id,points,place,tied"),
        ("core_onlinequal", "*"),
        ("core_debater", "id,latest_season"),
        ("core_team", "id,name,short_name"),
    ):
        left, right = [Counter(snapshot(path, table, fields)) for path in databases.values()]
        assert left == right, f"Nine-point rankings changed {table} differently"

    documents = []
    for engine, path in databases.items():
        target = path.with_name("sanitized.json")
        run("create_dev_fixtures", path, engine, "--output", str(target))
        documents.append(json.loads(target.read_text()))
    assert Counter(entry["model"] for entry in documents[0]) == Counter(entry["model"] for entry in documents[1])
    for model in ("core.user", "core.video", "core.round", "auth.group", "taggit.taggeditem"):
        left, right = [[entry for entry in document if entry["model"] == model] for document in documents]
        assert left == right, f"Fixture data for {model} differs"
    generated = [row["fields"]["code"] for row in documents[1] if row["model"] == "core.generatedcode"]
    assert len(generated) >= 2 and all(not code.startswith("PARITY") for code in generated)

    for engine, path in databases.items():
        with closing(sqlite3.connect(path)) as connection:
            username, = connection.execute(
                "SELECT username FROM core_user WHERE is_active AND is_superuser ORDER BY id LIMIT 1"
            ).fetchone()
        output = run("agent_credential", path, engine, "--admin", username, "--name", "Local fixture", "--write")
        banner, token = output.splitlines()
        number = int(banner.split()[1].removesuffix(":"))
        with closing(sqlite3.connect(path)) as connection:
            digest, can_write = connection.execute(
                "SELECT token_hash,can_write FROM core_agentcredential WHERE id=?", (number,)
            ).fetchone()
        assert hashlib.sha256(token.encode()).hexdigest() == digest and can_write
        assert run("agent_credential", path, engine, "--admin", username, "--revoke", str(number)) == "Credential revoked."
        with closing(sqlite3.connect(path)) as connection:
            revoked, = connection.execute("SELECT revoked_at FROM core_agentcredential WHERE id=?", (number,)).fetchone()
        assert revoked


def main():
    fixture = Path(os.environ["BR_STATE"]) / "dev.sqlite3"
    with TemporaryDirectory(prefix="black-rod-cli-parity-") as folder:
        databases = {}
        for engine in ("django", "rust"):
            path = Path(folder) / engine / "dev.sqlite3"
            path.parent.mkdir()
            with closing(sqlite3.connect(f"{fixture.as_uri()}?mode=ro", uri=True)) as original, closing(sqlite3.connect(path)) as copy:
                original.backup(copy)
                copy.execute("DELETE FROM core_qualbar WHERE season IN ('2019','2022')")
                copy.execute("""UPDATE core_team SET synthetic=TRUE,name='Fixture team',short_name='Fixture'
                                WHERE id=(SELECT MIN(team_id) FROM core_team_debaters)""")
                copy.execute("""WITH attendance AS (
                                   SELECT td.debater_id,t.season FROM core_teamresult r
                                   JOIN core_team_debaters td ON td.team_id=r.team_id
                                   JOIN core_tournament t ON t.id=r.tournament_id UNION ALL
                                   SELECT r.debater_id,t.season FROM core_speakerresult r
                                   JOIN core_tournament t ON t.id=r.tournament_id
                               ), only_once AS (
                                   SELECT debater_id FROM attendance GROUP BY debater_id
                                   HAVING MIN(season)='2025' AND MAX(season)='2025'
                               )
                               UPDATE core_debater SET first_season=NULL, status=1
                               WHERE id=(SELECT MIN(id) FROM core_debater d
                                 JOIN only_once o ON o.debater_id=d.id WHERE NOT d.synthetic AND NOT d.temporary)""")
                copy.execute("""DELETE FROM core_qual WHERE season='2025' AND debater_id=(
                                   SELECT MIN(qp.debater_id) FROM core_qualpoints qp
                                   JOIN core_debater d ON d.id=qp.debater_id JOIN core_school s ON s.id=d.school_id
                                   WHERE qp.season='2025' AND qp.points>=11.5 AND s.included_in_oty
                               )""")
                users = copy.execute("SELECT id FROM core_user ORDER BY id LIMIT 2").fetchall()
                assert len(users) == 2, "The fixture must include two users."
                for index, (user,) in enumerate(users):
                    copy.execute("INSERT INTO core_generatedcode (user_id,code,created_at) VALUES (?,?,CURRENT_TIMESTAMP)",
                                 (user, f"PARITY{index:06}"))
                copy.commit()
            databases[engine] = path
        outputs = {engine: run("set_old_quals", path, engine)
                   for engine, path in databases.items()}
        assert outputs["django"] == outputs["rust"], outputs
        for engine, path in databases.items():
            run("export_generated_codes", path, engine, "--output", str(path.with_suffix(".csv")))
        a, b = [path.with_suffix(".csv").read_bytes() for path in databases.values()]
        assert a == b and a.startswith(b"code,username,email\r\n")
        canonical = []
        for engine, database in databases.items():
            path = database.with_suffix(".json")
            run("export_canonical_import_data", database, engine, "--starting-season", "2025", "--output", str(path))
            document = json.loads(path.read_text())
            del document["exported_at"]
            canonical.append(document)
        assert not list(differences(*canonical)), "Canonical export differs"
        for cmd, table, columns, variations in (
            ("seed_qual_bars", "core_qualbar", "season,points", [()]),
            ("rebuild_synthetic_team_names", "core_team", "id,name,short_name", [("--dry-run",), ()]),
            ("fix_novice_debaters", "core_debater", "id,first_season,latest_season,status,region",
             [("--dry-run","--season","2025"), ("--confirm","--season","2025")]),
            ("reconstruct_historical_point_quals", "core_qual", "season,debater_id,qual_type,points",
             [("--season","2020","--season","2025","--dry-run"), ("--season","2025")]),
            ("seed_season_filters", "core_debater", "id,first_season,latest_season",
             [("--latest-only", "--active-in-season", "2025"), ("--first-only",), ()]),
        ):
            for variation in variations:
                summaries = [run(cmd, path, engine, *variation) for engine, path in databases.items()]
                assert summaries[0] == summaries[1], (cmd, variation, summaries)
                before, after = [snapshot(path, table, columns) for path in databases.values()]
                different = next(((a, b) for a, b in zip(before, after, strict=True) if a != b), None)
                assert different is None, (cmd, variation, different)
        verify_late_commands(databases)
        print("Maintenance commands match Django output and database effects, including ranking scope, sanitized fixtures, and credentials.")


if __name__ == "__main__":
    main()

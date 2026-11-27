"""Compare maintenance commands on separate copies of the local Django fixture."""
from contextlib import closing
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
        print("Eight maintenance commands match Django command output and database results.")


if __name__ == "__main__":
    main()

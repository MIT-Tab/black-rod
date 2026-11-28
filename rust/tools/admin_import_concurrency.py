"""Verify PostgreSQL admin form validation and safe ID allocation during imports."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup
from openpyxl import Workbook
import psycopg2
import requests

from agent_write_parity import fixture_database
from compare import HOST, ROOT, RUST_BIN, servers

SEED = """
import json
from reference import application

from datetime import date
from django.contrib.auth.models import Permission
from django.contrib.sessions.backends.db import SessionStore
from core.models import Debater, School, Team, TeamResult, Tournament, User

user = User.objects.create_superuser(username="admin_import_fixture")
session = SessionStore()
session["_auth_user_id"] = str(user.pk)
session["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
session["_auth_user_hash"] = user.get_session_auth_hash()
session.save()
school = School.objects.create(name="Admin import relationship fixture")
temporary_school = School.all_objects.create(name="Pending school fixture", temporary=True)
temporary_debater = Debater.all_objects.create(first_name="Pending", last_name="Debater", temporary=True)
staff = User.objects.create(username="admin_import_staff_fixture", is_staff=True)
staff.user_permissions.add(*Permission.objects.filter(
    content_type__app_label="core", codename__in=["view_school", "change_school", "delete_school", "view_debater"],
))
staff_session = SessionStore()
staff_session["_auth_user_id"] = str(staff.pk)
staff_session["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
staff_session["_auth_user_hash"] = staff.get_session_auth_hash()
staff_session.save()
admin_scope = [staff_session.session_key, school.pk, temporary_school.pk, temporary_debater.pk]
tournament = Tournament.objects.create(host=school, season="2026", date=date(2026, 11, 13))
team = Team.objects.create(name="Admin import fixture team")
award = TeamResult.objects.create(tournament=tournament, team=team, place=1)
deletions = []
for side in ("Django", "Rust"):
    source = School.objects.create(name=f"{side} concurrent deletion source")
    destination = School.objects.create(name=f"{side} concurrent deletion destination")
    debater = Debater.objects.create(first_name="Moving", last_name="Fixture", school=source)
    deletions.append([source.pk, destination.pk, debater.pk])
dupes = []
for side in ("Django", "Rust"):
    keeper = Debater.objects.create(first_name="Cleanup", last_name=side, school=school)
    candidate = Debater.objects.create(first_name="Cleanup", last_name=side, school=school)
    linked_team = Team.objects.create(name=f"{side} cleanup fixture")
    linked_team.debaters.add(keeper)
    dupes.append([keeper.pk, candidate.pk, linked_team.pk])
print(json.dumps([session.session_key, tournament.pk, award.pk, deletions, dupes, admin_scope]))
"""
IMPORT = "/admin/core/school/import/"
AUTO_NAME = "Automatic sequence fixture"
ADVISORY_KEY = 1413301


def insert_school(connection, name):
    with connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO core_school (name, short_name, included_in_oty, temporary, synthetic)
            VALUES (%s, %s, TRUE, FALSE, FALSE) RETURNING id""",
            (name, name),
        )
        return cursor.fetchone()[0]


def query(connection, sql, *args):
    with connection.cursor() as cursor:
        cursor.execute(sql, args)
        return cursor.fetchone()[0] if cursor.description else None


def sequence_value(connection):
    return query(connection, "SELECT pg_sequence_last_value(pg_get_serial_sequence('core_school','id')::regclass)")


def wait_for_lock(connection, sql, *args):
    deadline = monotonic() + 10
    while monotonic() < deadline:
        if query(connection, sql, *args):
            return
        sleep(0.03)
    raise AssertionError("The request did not reach the expected PostgreSQL lock")


def preview(client, root, name, school_id=None):
    return preview_rows(client, root, [(school_id, name)])


def preview_rows(client, root, rows):
    url = root + IMPORT
    form = BeautifulSoup(client.get(url, timeout=30).content, "html.parser")
    csrf = form.select_one("input[name=csrfmiddlewaretoken]")["value"]
    content = "id,name,short_name,included_in_oty,temporary,synthetic\n"
    for row_id, name in rows:
        content += f"{'' if row_id is None else row_id},{name},Fixture,1,0,0\n"
    response = client.post(
        url,
        data={"csrfmiddlewaretoken": csrf, "input_format": "0"},
        files={"import_file": ("fixture.csv", content, "text/csv")},
        timeout=30,
    )
    response.raise_for_status()
    markup = BeautifulSoup(response.content, "html.parser")
    confirm = markup.select_one('input[name="confirm"]')
    assert confirm is not None, f"The preview for {rows} did not render a confirmation form"
    form = confirm.find_parent("form")
    return urljoin(url, form["action"]), {item["name"]: item["value"] for item in form.select("input[name]")}


def confirm(client, draft):
    url, fields = draft
    response = client.post(url, data=fields, allow_redirects=False, timeout=30)
    assert response.status_code == 302, f"Import confirmation returned HTTP {response.status_code}"


def exercise(client, root, control, writer):
    automatic = preview(client, root, AUTO_NAME)
    query(control, f"""CREATE FUNCTION hold_fixture_insert() RETURNS TRIGGER LANGUAGE plpgsql AS $$
        BEGIN IF NEW.name = '{AUTO_NAME}' THEN
            PERFORM pg_advisory_xact_lock({ADVISORY_KEY});
        END IF; RETURN NEW; END;
        $$""")
    query(control, """CREATE TRIGGER hold_fixture_school BEFORE INSERT ON core_school
        FOR EACH ROW EXECUTE FUNCTION hold_fixture_insert()""")
    with ThreadPoolExecutor(max_workers=1) as executor:
        query(control, "SELECT pg_advisory_lock(%s)", ADVISORY_KEY)
        automatic_import = executor.submit(confirm, client, automatic)
        try:
            wait_for_lock(control, """SELECT EXISTS (SELECT 1 FROM pg_locks
                WHERE locktype='advisory' AND objid=%s AND NOT granted)""", ADVISORY_KEY)
            concurrent = insert_school(writer, "Concurrent automatic school fixture")
        finally:
            query(control, "SELECT pg_advisory_unlock(%s)", ADVISORY_KEY)
        automatic_import.result()
        imported = query(control, "SELECT id FROM core_school WHERE name=%s", AUTO_NAME)
        assert imported < concurrent <= sequence_value(control)
        assert insert_school(control, "Next ordinary writer fixture") > concurrent
        writer.commit()

        explicit_id = sequence_value(control) + 500
        explicit = preview(client, root, "Explicit sequence fixture", explicit_id)
        insert_school(writer, "Writer before the explicit import fixture")
        explicit_import = executor.submit(confirm, client, explicit)
        try:
            wait_for_lock(control, """SELECT EXISTS (SELECT 1 FROM pg_locks
                WHERE relation='core_school'::regclass AND mode='ShareRowExclusiveLock' AND NOT granted)""")
        finally:
            writer.commit()
        explicit_import.result()
        assert insert_school(control, "Writer after the explicit import fixture") > explicit_id

    reserved = query(control, """SELECT MAX(nextval(pg_get_serial_sequence('core_school','id')))
        FROM generate_series(1,10)""")
    confirm(client, preview(client, root, "Explicit ID below reserved sequence", reserved - 2))
    assert sequence_value(control) == reserved
    assert insert_school(control, "Writer after the lower explicit ID fixture") > reserved


    explicit = sequence_value(control) + 2
    combine = [
        (None, "Mixed import automatic before"), (explicit, "Mixed import explicit"),
        (None, "Mixed import automatic after"),
    ]
    mixed = preview_rows(client, root, combine)
    assert sequence_value(control) == explicit - 2, "Preview moved the database sequence"
    confirm(client, mixed)
    for assigned_id, name in ((explicit + 1, combine[0][1]), combine[1], (explicit + 2, combine[2][1])):
        assert query(control, "SELECT id FROM core_school WHERE name=%s", name) == assigned_id
    assert insert_school(control, "Writer after the mixed import fixture") > explicit + 2


def upload(client, root, path, file, contents, format_index):
    url = root + path
    document = BeautifulSoup(client.get(url, timeout=30).content, "html.parser")
    response = client.post(url, data={
        "csrfmiddlewaretoken": document.select_one("input[name=csrfmiddlewaretoken]")["value"],
        "input_format": format_index,
    }, files={"import_file": (file, contents)}, timeout=30)
    assert response.status_code == 200
    return BeautifulSoup(response.content, "html.parser")


def validate_import_cells(django, rust, session, tournament, award, control):
    attempts = [
        ("/admin/core/tournament/import/", f"id,date\n{tournament},2026-10-05 or 2026-10-06\n"),
        ("/admin/core/tournament/import/", f"id,date\n{tournament},2026-10-05 14:30:00\n"),
        ("/admin/core/teamresult/import/", f"id,tournament\n{award},{tournament}.7\n"),
        ("/admin/core/teamresult/import/", f"id,tournament\n{award},999999999\n"),
    ]

    for server in (django, rust):
        root = f"http://{HOST}:{server.port}"
        with requests.Session() as client:
            client.cookies.set("sessionid", session)
            for path, contents in attempts:
                page = upload(client, root, path, "invalid.csv", contents, "0")
                assert "error" in page.select_one("#content").get_text().lower(), (server.name, path)
                assert page.select_one('input[name="import_file_name"]') is None, (server.name, path)
                assert query(control, "SELECT tournament_id FROM core_teamresult WHERE id=%s", award) == tournament
                assert str(query(control, "SELECT date FROM core_tournament WHERE id=%s", tournament)) == "2026-11-13"

            book = Workbook()
            book.active.append(["id", "date"])
            book.active.append([tournament, datetime(2026, 10, 5, 14, 30)])
            contents = BytesIO()
            book.save(contents)
            path = "/admin/core/tournament/import/"
            page = upload(client, root, path, "fixture.xlsx", contents.getvalue(), "2")
            field = page.select_one('input[name="import_file_name"]')
            assert field is not None, (server.name, "workbook datetime could not be previewed")
            if server is rust:
                form = field.find_parent("form")
                draft = (urljoin(root + path, form["action"]), {
                    item["name"]: item.get("value", "") for item in form.select('input[type="hidden"][name]')
                })
                confirm(client, draft)
                assert str(query(control, "SELECT date FROM core_tournament WHERE id=%s", tournament)) == "2026-10-05"


def validate_import_debater_seasons(django, rust, session, control):
    path = "/admin/core/debater/import/"
    first_year = query(control, "SELECT first_season FROM core_debater WHERE first_name='Moving' AND last_name='Fixture' LIMIT 1")
    assert first_year is not None
    for server in (django, rust):
        root = f"http://{HOST}:{server.port}"
        explicit_id = query(control, "SELECT MAX(id) + 100 FROM core_debater")
        surname = f"ImportSeason{server.name}"
        contents = f"id,first_name,last_name\n{explicit_id},Historical,{surname}\n,Automatic,{surname}\n"
        with requests.Session() as client:
            client.cookies.set("sessionid", session)
            page = upload(client, root, path, "debaters.csv", contents, "0")
            field = page.select_one('input[name="import_file_name"]')
            assert field is not None, (server.name, page.select_one("#content").get_text(" ", strip=True))
            assert query(control, "SELECT COUNT(*) FROM core_debater WHERE last_name=%s", surname) == 0
            form = field.find_parent("form")
            confirm(client, (urljoin(root + path, form["action"]), {
                item["name"]: item.get("value", "") for item in form.select('input[type="hidden"][name]')
            }))
        assert query(control, """SELECT ARRAY[first_season, latest_season] FROM core_debater
            WHERE id=%s AND first_name='Historical' AND last_name=%s""", explicit_id, surname) == [None, None], server.name
        assert query(control, """SELECT ARRAY[first_season, latest_season] FROM core_debater
            WHERE first_name='Automatic' AND last_name=%s""", surname) == [first_year, first_year], server.name


def validate_admin_integer_fields(django, rust, session, tournament, db):
    school = query(db, "SELECT host_id FROM core_tournament WHERE id=%s", tournament)
    qual_type = query(db, "SELECT qual_type FROM core_tournament WHERE id=%s", tournament)
    for server in (django, rust):
        url = f"http://{HOST}:{server.port}/admin/core/tournament/{tournament}/change/"
        previous = query(db, "SELECT num_teams FROM core_tournament WHERE id=%s", tournament)
        with requests.Session() as client:
            client.cookies.set("sessionid", session)
            form = client.get(url, timeout=30)
            assert form.status_code == 200
            token = BeautifulSoup(form.content, "html.parser").select_one('input[name=csrfmiddlewaretoken]')["value"]
            fields = {
                "csrfmiddlewaretoken": token, "host": school, "name": "Numeric admin fixture",
                "manual_name": "Numeric admin fixture", "short_name": "Numeric admin fixture",
                "season": "2026", "date": "11/27/26", "qual_type": qual_type, "name_suffix": "0",
                "num_novice_teams": 0, "num_debaters": 0, "num_novice_debaters": 0,
                "num_rounds": 6, "autoqual_bar": 0, "_continue": "Save",
            }
            for value, message in (
                ("2147483648", "Ensure this value is less than or equal to 2147483647."),
                ("-2147483649", "Ensure this value is greater than or equal to -2147483648."),
            ):
                rejected = client.post(url, data={**fields, "num_teams": value}, timeout=30, allow_redirects=False)
                assert rejected.status_code == 200 and message in rejected.text, (server.name, value, rejected.status_code)
                assert query(db, "SELECT num_teams FROM core_tournament WHERE id=%s", tournament) == previous
            saved = client.post(url, data={**fields, "num_teams": "48.00"}, timeout=30, allow_redirects=False)
            assert saved.status_code == 302, (server.name, saved.status_code)
            assert query(db, "SELECT num_teams FROM core_tournament WHERE id=%s", tournament) == 48
            assert str(query(db, "SELECT date FROM core_tournament WHERE id=%s", tournament)) == "2026-11-27"
    print("PostgreSQL admin forms validate integer bounds and normalize valid inputs")


def validate_admin_scope(servers, scope):
    session, school, temporary_school, temporary_debater = scope
    for server in servers:
        root = f"http://{HOST}:{server.port}/admin/core"
        with requests.Session() as client:
            client.cookies.set("sessionid", session)
            for action in ("change", "delete", "history"):
                available = client.get(f"{root}/school/{school}/{action}/", allow_redirects=False, timeout=30)
                assert available.status_code == 200, (server.name, action, available.status_code)
                unavailable = client.get(f"{root}/school/{temporary_school}/{action}/", allow_redirects=False, timeout=30)
                assert (unavailable.status_code, unavailable.headers.get("Location")) == (302, "/admin/"), (
                    server.name, action, unavailable.status_code,
                )
            debater = client.get(f"{root}/debater/{temporary_debater}/change/", allow_redirects=False, timeout=30)
            assert debater.status_code == 200, (server.name, debater.status_code)


def validate_admin_filter_redirects(servers, session, control):
    model = "/admin/core/school/"
    filters = {"q": "Fixture café", "included_in_oty__exact": "1", "p": "2", "o": "-1"}
    preserved = {"_changelist_filters": urlencode(filters)}
    for server in servers:
        root = f"http://{HOST}:{server.port}"
        name = f"{server.name} filtered-return fixture"
        school = insert_school(control, name)
        change = f"{model}{school}/change/"
        url = f"{root}{change}?{urlencode(preserved)}"
        with requests.Session() as client:
            client.cookies.set("sessionid", session)
            page = BeautifulSoup(client.get(url, timeout=30).content, "html.parser")
            token = page.select_one('input[name="csrfmiddlewaretoken"]')["value"]
            for selector, action in ((".historylink", "history"), (".deletelink", "delete")):
                link = urlsplit(page.select_one(selector)["href"])
                assert (link.path, dict(parse_qsl(link.query))) == (f"{model}{school}/{action}/", preserved), server.name
            for submit, path, query_data in (
                ("_save", model, filters),
                ("_continue", change, preserved),
                ("_addanother", f"{model}add/", preserved),
            ):
                result = client.post(url, data={
                    "csrfmiddlewaretoken": token, "name": name, "short_name": name,
                    "included_in_oty": "on", submit: "1",
                }, allow_redirects=False, timeout=30)
                assert result.status_code == 302, (server.name, submit, result.status_code)
                location = urlsplit(result.headers["Location"])
                assert (location.path, dict(parse_qsl(location.query))) == (path, query_data), (server.name, submit, location)


def validate_concurrent_deletion(servers, session, deletions, control, writer):
    for server, (source, destination, debater) in zip(servers, deletions):
        path = f"http://{HOST}:{server.port}/core/schools/{source}/delete"
        with requests.Session() as client, ThreadPoolExecutor(max_workers=1) as pool:
            client.cookies.set("sessionid", session)
            form = client.get(path, timeout=15)
            form.raise_for_status()
            token = BeautifulSoup(form.content, "html.parser").select_one("input[name=csrfmiddlewaretoken]")["value"]
            query(writer, "UPDATE core_debater SET school_id=%s WHERE id=%s", destination, debater)
            deleting = pool.submit(client.post, path, data={"csrfmiddlewaretoken": token},
                                   allow_redirects=False, timeout=25)
            try:
                wait_for_lock(control, """
                    SELECT EXISTS (SELECT 1 FROM pg_stat_activity
                    WHERE datname=current_database() AND wait_event_type='Lock' AND query ILIKE %s)
                """, "%core_debater%")
            finally:
                writer.commit()
            assert deleting.result().status_code == 302, server.name
            assert query(control, "SELECT school_id FROM core_debater WHERE id=%s", debater) == destination, server.name
            assert not query(control, "SELECT EXISTS (SELECT 1 FROM core_school WHERE id=%s)", source), server.name


def validate_cleanup_during_import(env, clone, dupes, control, writer):
    django = [str(ROOT / "venv/bin/python"), "-c",
              "from reference import application; from django.core.management import call_command; "
              "import sys; call_command('clean_duplicate_debaters', *sys.argv[1:])"]
    rust = [str(RUST_BIN), "clean_duplicate_debaters"]
    command_env = {**env, "BLACK_ROD_DATABASE_URL": clone, "BLACK_ROD_PARITY_POSTGRES_URL": clone,
                   "DOT_ENV_FILE": os.devnull, "LIVE_MITTAB_DIRECTORY_URL": ""}
    for command, (minimum, candidate, team) in zip((rust, django), reversed(dupes)):
        query(writer, "SELECT id FROM core_debater WHERE id=%s FOR UPDATE", candidate)
        query(writer, "INSERT INTO core_team_debaters (team_id,debater_id) VALUES (%s,%s)", team, candidate)
        with ThreadPoolExecutor(max_workers=1) as pool:
            cleanup = pool.submit(
                subprocess.run, [*command, "--min-id", str(minimum), "--confirm"],
                env=command_env, capture_output=True, text=True, timeout=25,
            )
            try:
                wait_for_lock(control, """
                    SELECT EXISTS (SELECT 1 FROM pg_stat_activity
                    WHERE datname=current_database() AND wait_event_type='Lock' AND query ILIKE %s)
                """, "%core_debater%")
            finally:
                writer.commit()
            result = cleanup.result()
        assert result.returncode == 0, result.stderr
        assert query(control, "SELECT EXISTS(SELECT 1 FROM core_team_debaters WHERE team_id=%s AND debater_id=%s)",
                     team, candidate), "Cleanup discarded a newly linked debater"


def main():
    postgres = os.environ["BLACK_ROD_PARITY_POSTGRES_URL"]
    seed = Path(os.environ["BR_STATE"]) / "dev.sqlite3"
    with TemporaryDirectory(prefix="black-rod-admin-import-") as folder:
        database = Path(folder) / "dev.sqlite3"
        with fixture_database(seed, database, postgres):
            os.environ.update(BR_STATE=folder, BLACK_ROD_PARITY_WRITABLE="1")
            env = {**os.environ, "SECRET_KEY": "disposable-local-parity-key",
                   "PYTHONPATH": f"{Path(__file__).resolve().parent}{os.pathsep}{os.environ['PYTHONPATH']}"}
            data = subprocess.check_output([str(ROOT / "venv/bin/python"), "-c", SEED], env=env, text=True)
            session, tournament, award, deletions, dupes, admin_scope = json.loads(data.splitlines()[-1])
            clone = os.environ["BLACK_ROD_PARITY_POSTGRES_URL"]
            with servers(database) as (django, rust), requests.Session() as client, closing(
                psycopg2.connect(clone)
            ) as control, closing(psycopg2.connect(clone)) as writer:
                client.cookies.set("sessionid", session)
                control.autocommit = True
                validate_admin_scope((django, rust), admin_scope)
                validate_admin_filter_redirects((django, rust), session, control)
                validate_import_cells(django, rust, session, tournament, award, control)
                validate_import_debater_seasons(django, rust, session, control)
                validate_admin_integer_fields(django, rust, session, tournament, control)
                exercise(client, f"http://{HOST}:{rust.port}", control, writer)
                validate_concurrent_deletion((django, rust), session, deletions, control, writer)
                validate_cleanup_during_import(env, clone, dupes, control, writer)
    print("PostgreSQL admin imports validate complete dates and foreign keys, and advance IDs safely")
    print("School deletion and duplicate cleanup preserve concurrent affiliations and memberships")


if __name__ == "__main__":
    main()

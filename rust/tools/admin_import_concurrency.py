"""Verify PostgreSQL admin imports leave IDs safe for ordinary concurrent writes."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from urllib.parse import urljoin

from bs4 import BeautifulSoup
import psycopg2
import requests

from agent_write_parity import fixture_database
from compare import HOST, ROOT, servers

SEED = """
from reference import application

from django.contrib.sessions.backends.db import SessionStore
from core.models import User

user = User.objects.create_superuser(username="admin_import_fixture")
session = SessionStore()
session["_auth_user_id"] = str(user.pk)
session["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
session["_auth_user_hash"] = user.get_session_auth_hash()
session.save()
print(session.session_key)
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
    raise AssertionError("The import did not reach the expected PostgreSQL lock")


def preview(client, root, name, school_id=None):
    url = root + IMPORT
    form = BeautifulSoup(client.get(url, timeout=30).content, "html.parser")
    csrf = form.select_one("input[name=csrfmiddlewaretoken]")["value"]
    row_id = "" if school_id is None else school_id
    content = f"id,name,short_name,included_in_oty,temporary,synthetic\n{row_id},{name},Fixture,1,0,0\n"
    response = client.post(
        url,
        data={"csrfmiddlewaretoken": csrf, "input_format": "0"},
        files={"import_file": ("fixture.csv", content, "text/csv")},
        timeout=30,
    )
    response.raise_for_status()
    markup = BeautifulSoup(response.content, "html.parser")
    confirm = markup.select_one('input[name="confirm"]')
    assert confirm is not None, f"The preview for {name} did not render a confirmation form"
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


def main():
    postgres = os.environ["BLACK_ROD_PARITY_POSTGRES_URL"]
    seed = Path(os.environ["BR_STATE"]) / "dev.sqlite3"
    with TemporaryDirectory(prefix="black-rod-admin-import-") as folder:
        database = Path(folder) / "dev.sqlite3"
        with fixture_database(seed, database, postgres):
            os.environ.update(BR_STATE=folder, BLACK_ROD_PARITY_WRITABLE="1")
            env = {**os.environ, "SECRET_KEY": "disposable-local-parity-key",
                   "PYTHONPATH": f"{Path(__file__).resolve().parent}{os.pathsep}{os.environ['PYTHONPATH']}"}
            session = subprocess.check_output([str(ROOT / "venv/bin/python"), "-c", SEED], env=env, text=True)
            clone = os.environ["BLACK_ROD_PARITY_POSTGRES_URL"]
            with servers(database) as (_, rust), requests.Session() as client, closing(
                psycopg2.connect(clone)
            ) as control, closing(psycopg2.connect(clone)) as writer:
                client.cookies.set("sessionid", session.splitlines()[-1])
                control.autocommit = True
                exercise(client, f"http://{HOST}:{rust.port}", control, writer)
    print("PostgreSQL admin import IDs advance safely across automatic and explicit concurrent writes")


if __name__ == "__main__":
    main()

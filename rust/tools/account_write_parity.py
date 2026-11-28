"""Check that overlapping email edits preserve one consistent account identity."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic, sleep

from bs4 import BeautifulSoup
import psycopg2
import requests

from agent_write_parity import fixture_database
from compare import HOST, ROOT, RUST_BIN, available_port, servers, start_server

SEED = """
from reference import application
from allauth.account.models import EmailAddress
from django.contrib.sessions.backends.db import SessionStore
from core.models import User
import json

user = User.objects.create_user(username="email-write-parity-fixture", email="primary@example.test")
EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
EmailAddress.objects.create(user=user, email="alternate@example.test", verified=True)
session = SessionStore()
session.update({"_auth_user_id": str(user.pk), "_auth_user_backend": "django.contrib.auth.backends.ModelBackend",
                "_auth_user_hash": user.get_session_auth_hash()})
session.save()
print(json.dumps([user.id, session.session_key]))
"""

PRIMARY = "primary@example.test"
ALTERNATE = "alternate@example.test"


def wait_for_account_lock(observer, pending):
    deadline = monotonic() + 10
    with observer.cursor() as cursor:
        while monotonic() < deadline:
            cursor.execute("""
                SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname=current_database()
                    AND wait_event_type='Lock' AND query LIKE 'UPDATE core_user SET id=id%')
            """)
            if cursor.fetchone()[0]:
                return
            assert not pending.done(), "The email edit bypassed the account lock"
            sleep(0.025)
    raise AssertionError("The email edit did not reach the account lock")


def exercise(server, user, session, control, observer):
    url = f"http://{HOST}:{server.port}/accounts/email/"
    with requests.Session() as client, control.cursor() as db, ThreadPoolExecutor(max_workers=1) as workers:
        client.cookies.set("sessionid", session)
        form = BeautifulSoup(client.get(url, timeout=15).content, "html.parser")
        csrf = form.select_one('input[name=csrfmiddlewaretoken]')["value"]
        for action in ("action_primary", "action_remove"):
            db.execute("SELECT id FROM core_user WHERE id=%s FOR UPDATE", (user,))
            pending = workers.submit(client.post, url, timeout=20, allow_redirects=False,
                                     data={"csrfmiddlewaretoken": csrf, "email": ALTERNATE, action: "1"})
            try:
                wait_for_account_lock(observer, pending)
                if action == "action_primary":
                    db.execute("DELETE FROM account_emailaddress WHERE user_id=%s AND email=%s", (user, ALTERNATE))
                    expected = [(PRIMARY, True)]
                else:
                    db.execute('UPDATE account_emailaddress SET "primary"=FALSE WHERE user_id=%s', (user,))
                    db.execute('UPDATE account_emailaddress SET "primary"=TRUE WHERE user_id=%s AND email=%s',
                               (user, ALTERNATE))
                    db.execute("UPDATE core_user SET email=%s WHERE id=%s", (ALTERNATE, user))
                    expected = [(ALTERNATE, True), (PRIMARY, False)]
            finally:
                control.commit()
            result = pending.result(timeout=20)
            assert result.status_code == 200, (action, result.status_code)
            db.execute('SELECT email,"primary" FROM account_emailaddress WHERE user_id=%s ORDER BY email', (user,))
            assert db.fetchall() == expected, action
            db.execute("SELECT email FROM core_user WHERE id=%s", (user,))
            assert db.fetchone() == (expected[0][0],), action
            if action == "action_primary":
                db.execute('INSERT INTO account_emailaddress (user_id,email,verified,"primary") VALUES (%s,%s,TRUE,FALSE)',
                           (user, ALTERNATE))
                control.commit()


def signup_when_mail_is_unavailable(database):
    port = available_port()
    root = f"http://{HOST}:{port}"
    config = {
        "ENV": "production", "SECRET_KEY": "disposable-local-parity-key",
        "DOT_ENV_FILE": os.devnull, "BLACK_ROD_CURRENT_SEASON": "2026",
        "BLACK_ROD_DATABASE_URL": database, "BLACK_ROD_BIND": f"{HOST}:{port}",
        "BLACK_ROD_DIRECT_CONNECTIONS": "1", "BLACK_ROD_ALLOWED_HOSTS": HOST,
        "BLACK_ROD_PUBLIC_ORIGIN": root, "SENTRY_DSN": "", "LIVE_MITTAB_DIRECTORY_URL": "",
        "BLACK_ROD_MEMCACHED_ADDR": f"{HOST}:{available_port()}",
        "BLACK_ROD_SMTP_URL": f"smtp://{HOST}:{available_port()}",
    }
    with start_server("Rust", [str(RUST_BIN)], port, config), requests.Session() as client:
        signup = root + "/accounts/signup/?next=/accounts/email/"
        form = BeautifulSoup(client.get(signup, timeout=15).content, "html.parser")
        csrf = form.select_one('input[name=csrfmiddlewaretoken]')["value"]
        result = client.post(signup, timeout=25, allow_redirects=False, data={
            "csrfmiddlewaretoken": csrf, "username": "delivery-failure-fixture",
            "email": "unavailable@example.test", "password1": "W7fish!test-river-pony-93",
            "password2": "W7fish!test-river-pony-93", "next": "/accounts/email/",
        })
        assert result.status_code == 302 and client.cookies.get("sessionid"), result.status_code
        addresses = client.get(root + "/accounts/email/", timeout=15)
        assert addresses.status_code == 200 and "unavailable@example.test" in addresses.text
        assert "We could not send a confirmation email." in addresses.text


def main():
    postgres = os.environ["BLACK_ROD_PARITY_POSTGRES_URL"]
    seed = Path(os.environ["BR_STATE"]) / "dev.sqlite3"
    with TemporaryDirectory(prefix="standings-account-write-") as folder:
        database = Path(folder) / "dev.sqlite3"
        with fixture_database(seed, database, postgres):
            target = os.environ["BLACK_ROD_PARITY_POSTGRES_URL"]
            env = {**os.environ, "BR_STATE": folder, "SECRET_KEY": "disposable-local-parity-key",
                   "PYTHONPATH": f"{Path(__file__).resolve().parent}{os.pathsep}{os.environ['PYTHONPATH']}"}
            output = subprocess.check_output([str(ROOT / "venv/bin/python"), "-c", SEED], text=True, env=env)
            user, session = json.loads(output.splitlines()[-1])
            with servers(database) as (_, rust), closing(psycopg2.connect(target)) as control, closing(
                psycopg2.connect(target)
            ) as observer:
                observer.autocommit = True
                exercise(rust, user, session, control, observer)
            signup_when_mail_is_unavailable(target)
    print("Overlapping PostgreSQL email edits preserve the primary address, and signup survives mail failure")


if __name__ == "__main__":
    main()

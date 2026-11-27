"""Verify standings replay in Chrome against the same local Black Rod fixture."""

from argparse import ArgumentParser
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import zip_longest
import json
from pathlib import Path
import os
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.parse import parse_qs, urlencode, urlsplit

from PIL import Image, ImageChops
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait

from compare import HOST, ROOT, servers

CASE = "/standings/replay/?season=2025&default=coty"


def render(driver, server, dimensions, path):
    width, height = dimensions
    driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
        "width": width, "height": height, "deviceScaleFactor": 1, "mobile": False,
    })
    driver.get(f"http://{HOST}:{server.port}{CASE}")
    wait = WebDriverWait(driver, 20)
    wait.until(lambda browser: browser.execute_script("""
        return document.querySelector("#replay-slider")?.disabled === false
            && Number(document.querySelector("#replay-slider").max) > 0;
    """))
    driver.execute_script("""
        const slider = document.querySelector("#replay-slider");
        slider.value = slider.max;
        slider.dispatchEvent(new Event("input", {bubbles: true}));
    """)
    wait.until(lambda browser: browser.execute_script("""
        return document.querySelector("#replay-coty-body tr")?.children.length >= 4;
    """))
    driver.execute_async_script("document.fonts.ready.then(() => requestAnimationFrame(() => requestAnimationFrame(arguments[0])));")
    assert driver.find_element("css selector", ".tab-pane.active").get_attribute("id") == "replay-coty"
    assert driver.find_element("css selector", "#replay-speed-coty").is_enabled()
    assert "Login" in driver.find_element("tag name", "nav").get_attribute("textContent")
    assert driver.save_screenshot(str(path))

    state = driver.execute_script("""
        const slider = document.querySelector("#replay-slider");
        slider.value = slider.min;
        slider.dispatchEvent(new Event("input", {bubbles: true}));
        return {
            href: document.querySelector(".replay-season-dropdown a.dropdown-item").getAttribute("href"),
            maximum: slider.max,
            initial: document.querySelector("#replay-date-label").textContent.trim(),
            content: document.querySelector("#replay-coty-body").textContent.trim(),
        };
    """)
    driver.execute_script("""
        const picker = document.querySelector("#appearance-theme");
        picker.value = "dark";
        picker.dispatchEvent(new Event("change"));
    """)
    wait.until(lambda browser: browser.execute_script("return document.documentElement.dataset.theme") == "dark")
    driver.refresh()
    wait.until(lambda browser: browser.execute_script("return document.documentElement.dataset.theme") == "dark")
    driver.execute_script("localStorage.removeItem('theme');")
    return state


def home_state(driver, server):
    driver.get(f"http://{HOST}:{server.port}/?season=2025&default=soty")
    return driver.execute_script("""
        const compact = (node) => node?.textContent.replace(/\s+/g, " ").trim();
        return {
            title: document.title.trim(),
            active: document.querySelector(".tab-pane.active")?.id,
            teams: Array.from(document.querySelectorAll("#toty table tbody tr")).slice(0, 5).map(compact),
            speakers: Array.from(document.querySelectorAll("#soty table tbody tr")).slice(0, 5).map(compact),
            clubs: Array.from(document.querySelectorAll("#coty table tbody:not(.collapse) tr")).slice(0, 5).map(compact),
            selectedSeason: compact(document.querySelector("#toty .replay-season-picker .dropdown-toggle")),
            replay: document.querySelector("#toty .watch-replay-btn")?.getAttribute("href"),
        };
    """)


def directory_state(driver, server, path):
    driver.get(f"http://{HOST}:{server.port}{path}")
    return driver.execute_script("""
        const text = (node) => node.textContent.replace(/\s+/g, " ").trim();
        return {
            headings: Array.from(document.querySelectorAll(".container-fluid h3,.container-fluid h5,.container-fluid h6")).map(text),
            rows: Array.from(document.querySelectorAll("table tr")).slice(0, 24).map(row => Array.from(row.children).map(text)),
            links: Array.from(document.querySelectorAll("table a, .table-container nav .page-link"))
                .slice(0, 24).map(node => [node.getAttribute("href"), text(node)]),
        };
    """)


def assert_directories(driver, django, rust):
    paths = [
        "/core/schools/",
        "/core/schools/?name__icontains=harv&sort=-name",
        "/core/debaters/?first_name__icontains=an&status=1&sort=last_name&page=2",
        "/core/teams/?name__icontains=Yale&sort=name",
        "/core/schedules/?season=2025",
        "/core/schools/7?season=2025",
        "/stats/",
    ]
    for path in paths:
        expected = directory_state(driver, django, path)
        candidate = directory_state(driver, rust, path)
        for key in expected:
            for index, (left, right) in enumerate(zip_longest(expected[key], candidate[key])):
                assert left == right, f"{path} {key}[{index}] differed: {left!r} vs {right!r}"


def django_session():
    code = """
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django import setup
setup()
user = get_user_model().objects.get(username="tab")
session = SessionStore()
session["_auth_user_id"] = str(user.pk)
session["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
session["_auth_user_hash"] = user.get_session_auth_hash()
session.save()
print(session.session_key)
"""
    result = subprocess.check_output([str(ROOT / "venv/bin/python"), "-c", code],
                                     env={**os.environ, "SECRET_KEY": "disposable-local-parity-key"}, text=True)
    return result.splitlines()[-1]


def assert_session_nav(driver, servers):
    cookie = django_session()
    try:
        for server in servers:
            driver.get(f"http://{HOST}:{server.port}/games/")
            driver.add_cookie({"name": "sessionid", "value": cookie})
            driver.refresh()
            nav = driver.find_element("tag name", "nav").get_attribute("textContent")
            assert "My Account" in nav
            assert "Django Admin" in nav
            assert "Logout" in nav
            assert "Sign in to play." not in driver.find_element("tag name", "main").text
            driver.delete_cookie("sessionid")
    finally:
        code = """
import sys
from django import setup
setup()
from django.contrib.sessions.backends.db import SessionStore
SessionStore().delete(sys.stdin.read().strip())
"""
        subprocess.run([str(ROOT / "venv/bin/python"), "-c", code], input=cookie, text=True, check=True)


class OAuthProvider(BaseHTTPRequestHandler):
    def log_message(self, _format, *args):
        pass

    def reply(self, payload):
        payload = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        location = urlsplit(self.path)
        if location.path == "/oauth/authorize":
            params = parse_qs(location.query)
            redirect = params["redirect_uri"][0]
            assert urlsplit(redirect).hostname == HOST
            query = urlencode({"state": params["state"][0], "code": "fixture-code"})
            self.send_response(302)
            self.send_header("Location", f"{redirect}?{query}")
            self.end_headers()
        elif location.path == "/api/tournaments":
            self.reply({"tournaments": [{"url": f"http://{HOST}:{self.server.server_port}"}]})
        elif location.path == "/api/live-results":
            self.reply({
                "schema_version": 1, "source_url": f"http://{HOST}:{self.server.server_port}",
                "tournament": {"sanctioned": True, "season": "2026", "name": "Fixture Live",
                               "date": "2026-11-26", "tournament_type": 0, "num_teams": 36,
                               "host_school": {"apda_id": 7, "name": "Harvard"}},
                "team_results": [{"division": "varsity", "place": 1, "team": {"debaters": [
                    {"name": "Fixture First", "school": {"apda_id": 7, "name": "Ignore this name"}},
                    {"name": "Fixture Second", "school": {"apda_id": 7, "name": "Ignore this name"}},
                ]}}],
                "speaker_results": [{"division": "varsity", "place": 1, "debater":
                    {"name": "Fixture First", "school": {"apda_id": 7}}}],
            })
        elif location.path == "/oauth/me" and self.headers.get("Authorization") == "Bearer fixture-token":
            self.reply({"ID": 1234567, "user_nicename": "rustparity",
                        "user_email": "oauth-fixture@example.test", "display_name": "Example Tester",
                        "user_roles": ["private_side_viewer"]})
        else:
            self.send_error(404)

    def do_POST(self):
        values = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
        if self.path == "/oauth/token" and values.get("code") == ["fixture-code"]:
            self.reply({"access_token": "fixture-token", "expires_in": 600})
        else:
            self.send_error(400)


@contextmanager
def oauth_provider():
    with ThreadingHTTPServer((HOST, 0), OAuthProvider) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        origin = f"http://{HOST}:{server.server_port}"
        os.environ["BLACK_ROD_OAUTH_BASE"] = f"{origin}/"
        os.environ["LIVE_MITTAB_DIRECTORY_URL"] = f"{origin}/api/tournaments"
        thread.start()
        try:
            yield
        finally:
            server.shutdown()
            thread.join()


def assert_oauth_roundtrip(driver, rust, django):
    root = f"http://{HOST}:{rust.port}"
    driver.get(f"{root}/accounts/apdaonline/login/callback/?code=fixture-code&state=unrecognized")
    assert "Social Network Login Failure" in driver.find_element("tag name", "h1").text
    for _ in range(2):
        driver.get(f"{root}/auth/login/?next=/games/")
        driver.delete_cookie("sessionid")
        driver.execute_script("document.querySelector('#id_remember').checked = false;")
        submitter = driver.find_element("css selector", "button[name=provider]")
        driver.execute_script("arguments[0].form.requestSubmit(arguments[0]);", submitter)
        try:
            WebDriverWait(driver, 10).until(lambda browser: browser.current_url == f"{root}/games/")
        except TimeoutException as error:
            raise AssertionError(f"OAuth ended at {urlsplit(driver.current_url).path} with the page {driver.find_element('tag name', 'body').text[:180]!r}") from error
        cookie = driver.get_cookie("sessionid")
        assert cookie is not None and "expiry" not in cookie and cookie["httpOnly"]
        driver.get(f"http://{HOST}:{django.port}/games/")
        assert "My Account" in driver.find_element("tag name", "nav").get_attribute("textContent")
        driver.delete_cookie("sessionid")


def assert_login_roundtrip(driver, rust, django):
    root = f"http://{HOST}:{rust.port}"
    driver.get(f"{root}/auth/login/?next=/games/")
    driver.delete_cookie("sessionid")
    assert driver.find_element("id", "id_remember").is_selected()
    driver.find_element("id", "id_username").send_keys("tab")
    driver.find_element("id", "id_password").send_keys("password")
    driver.find_element("css selector", "#id_password").submit()
    WebDriverWait(driver, 10).until(lambda browser: browser.current_url == f"{root}/games/")
    persisted = driver.get_cookie("sessionid")
    assert persisted is not None and persisted.get("expiry") and persisted["httpOnly"]
    driver.get(f"http://{HOST}:{django.port}/games/")
    assert "My Account" in driver.find_element("tag name", "nav").get_attribute("textContent")
    driver.get(f"{root}/auth/login/?next=/games/")
    result = driver.execute_async_script("""
        const done = arguments[0];
        const csrf = document.querySelector('[name=csrfmiddlewaretoken]').value;
        fetch("/auth/logout/", {method: "POST", redirect: "manual", headers: {"X-CSRFToken": csrf}})
            .then(() => done(true), () => done(false));
    """)
    assert result and driver.get_cookie("sessionid") is None
    driver.get(f"http://{HOST}:{django.port}/games/")
    assert "Sign in to play." in driver.find_element("tag name", "main").text


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    database = (Path(os.environ["BR_STATE"]) / "dev.sqlite3").resolve(strict=True)
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--force-color-profile=srgb")
    options.add_argument("--disable-search-engine-choice-screen")
    options.add_argument("--no-sandbox")
    os.environ["BLACK_ROD_PARITY_STATIC"] = "1"
    os.environ["BLACK_ROD_PARITY_WRITABLE"] = "1"
    with TemporaryDirectory(prefix="black-rod-browser-") as state:
        browser_db = Path(state) / "dev.sqlite3"
        with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as source:
            with sqlite3.connect(browser_db) as copy:
                source.backup(copy)
        os.environ["BR_STATE"] = state
        browser_checks(browser_db, options, args.output)


def browser_checks(database, options, output):
    with oauth_provider(), servers(database) as (django, rust), webdriver.Chrome(options=options) as driver:
        expected_home = home_state(driver, django)
        result_home = home_state(driver, rust)
        assert expected_home["active"] == "soty"
        for key, result in expected_home.items():
            assert result_home[key] == result, f"Home page {key} differs: {result!r} vs {result_home[key]!r}"
        assert_session_nav(driver, (django, rust))
        assert_login_roundtrip(driver, rust, django)
        assert_oauth_roundtrip(driver, rust, django)
        assert_directories(driver, django, rust)
        live_django = directory_state(driver, django, "/live")
        live_rust = directory_state(driver, rust, "/live")
        assert any("Fixture First" in cell for row in live_django["rows"] for cell in row)
        assert live_django == live_rust, "Live results differed for the mock tournament"
        for mode, dimensions in [("desktop", (1440, 1000)), ("mobile", (390, 844))]:
            reference = output / f"{mode}-django.png"
            candidate = output / f"{mode}-rust.png"
            expected = render(driver, django, dimensions, reference)
            observed = render(driver, rust, dimensions, candidate)
            assert expected == observed, f"{mode} replay data or controls differ"
            with Image.open(reference) as left, Image.open(candidate) as right:
                difference = ImageChops.difference(left.convert("RGB"), right.convert("RGB"))
                mask = difference.convert("L").point(lambda value: int(value > 8))
                changed = mask.histogram()[1]
                fraction = changed / (left.width * left.height)
                print(f"{mode} replay changed pixels above tolerance {fraction:.3%}")
                assert fraction <= 0.01, f"{mode} replay screenshot differs by {fraction:.2%}"


if __name__ == "__main__":
    main()

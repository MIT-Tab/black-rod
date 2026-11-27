"""Verify standings replay in Chrome against the same local Black Rod fixture."""

from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
from email import policy
from email.parser import BytesParser
from contextlib import contextmanager
from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import zip_longest
import json
from pathlib import Path
from queue import Queue
import os
import socket
from socketserver import StreamRequestHandler, ThreadingTCPServer
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

from PIL import Image, ImageChops
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.expected_conditions import staleness_of
from selenium.webdriver.support.ui import WebDriverWait

from compare import Case, HOST, ROOT, RUST_BIN, compare as compare_responses, request, servers

CASE = "/standings/replay/?season=2025&default=coty"


def document_replaced(element):
    stale = staleness_of(element)

    def check(driver):
        try:
            return stale(driver)
        except WebDriverException as error:
            if "Node with given id does not belong to the document" in str(error):
                return True
            raise

    return check


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
    driver.execute_async_script("""
        const done = arguments[0];
        document.fonts.ready
            .then(() => Promise.allSettled(document.getAnimations().map(animation => animation.finished)))
            .then(() => requestAnimationFrame(() => requestAnimationFrame(done)));
    """)
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
        "/core/videos/?tournament__season=2025&sort=-id",
        "/core/videos/?round=9&sort=id",
        "/core/motions/?text__icontains=disposable&sort=text",
        "/core/motions/?topics=%22Fixture+topic%22",
        "/core/resources/?title__icontains=browser&sort=title",
        "/core/tournaments/?season=2025&name__icontains=harvard&sort=-points",
        "/core/tournaments/?season=2025&points_min=15.5&num_teams_max=75&sort=date",
        "/core/schools/7?season=2025",
        "/stats/",
    ]
    for path in paths:
        expected = directory_state(driver, django, path)
        candidate = directory_state(driver, rust, path)
        for key in expected:
            for index, (left, right) in enumerate(zip_longest(expected[key], candidate[key])):
                assert left == right, f"{path} {key}[{index}] differed: {left!r} vs {right!r}"


def assert_legacy_standings(driver, django, rust):
    for section, query in (
        ("soty", "season=2025"),
        ("toty", "season=2025"),
        ("noty", "season=2025"),
        ("coty", "season=2025"),
        ("soty", "season=2025&debater__school__name__icontains=Harvard&sort=-points"),
        ("toty", "season=2025&team__name__icontains=Harvard&sort=team"),
        ("coty", "season=2025&school__name__icontains=Harvard"),
        ("noty", "season=2024&page=2"),
        ("soty", ""),
    ):
        path = f"/core/{section}?{query}" if query else f"/core/{section}"
        expected = directory_state(driver, django, path)
        actual = directory_state(driver, rust, path)
        for key in expected:
            for index, (left, right) in enumerate(zip_longest(expected[key], actual[key])):
                assert left == right, (path, key, index, left, right)


def assert_partnership_paths(driver, django, rust):
    root = "/core/debaters/partnership-paths/"
    for query in ("", "?first_debater=5270&second_debater=6015",
                  "?first_debater=5270&second_debater=5270",
                  "?first_debater=0&second_debater="):
        values = []
        for server in (django, rust):
            driver.get(f"http://{HOST}:{server.port}{root}{query}")
            values.append(driver.execute_script("""
                const main = document.querySelector("main");
                if (!main) return null;
                const text = node => node.textContent.replace(/\s+/g, " ").trim();
                return {
                    title: main.querySelector("h1")?.textContent.trim(),
                    links: Array.from(main.querySelectorAll("nav a, ol a")).map(link =>
                        [link.getAttribute("href"), link.textContent.trim()]),
                    rows: Array.from(main.querySelectorAll("ol li, .invalid-feedback")).map(text),
                    selects: Array.from(main.querySelectorAll("select")).map(field => [field.name, field.value]),
                };
            """))
        assert values[0] and values[0] == values[1], (query, values)
    for server in (django, rust):
        driver.get(f"http://{HOST}:{server.port}{root}network/?x_axis=first_year&from_season=2025")
        assert driver.find_element("id", "id_x_axis").get_attribute("value") == "first_year"
        assert driver.find_element("id", "partnership-network").get_attribute("data-url").endswith(
            "/network/data/?x_axis=first_year&from_season=2025")
        WebDriverWait(driver, 15).until(lambda session: session.find_elements("css selector", "#partnership-network canvas"))


def assert_partnership_stats(driver, django, rust):
    root = "/core/debaters/partnership-paths/"
    for view in ("person/?debater=5270", "stats/results/"):
        states = []
        for server in (django, rust):
            driver.get(f"http://{HOST}:{server.port}{root}{view}")
            states.append(driver.execute_script("""
                const text = node => node.textContent.replace(/\s+/g, " ").trim();
                return {
                    headings: Array.from(document.querySelectorAll("h1,h2,h3")).map(text),
                    rows: Array.from(document.querySelectorAll("main li, section li")).map(text),
                    links: Array.from(document.querySelectorAll("main li a, section li a"))
                        .map(link => [link.getAttribute("href"), text(link)]),
                };
            """))
        assert states[0]["headings"], view
        for key, values in states[0].items():
            other = states[1][key]
            assert len(values) == len(other), (view, key, len(values), len(other))
            for index, (left, right) in enumerate(zip(values, other)):
                assert left == right, (view, key, index, left, right)


def assert_shared_partnership_cache(driver, django, rust):
    import pickle
    from uuid import UUID, uuid4

    host, port = os.environ["BLACK_ROD_PARITY_MEMCACHED_ADDR"].split(":")
    with socket.create_connection((host, int(port)), timeout=5) as connection, connection.makefile("rwb") as cache:
        def command(line, body=b""):
            cache.write(line.encode() + b"\r\n" + body)
            cache.flush()
            return cache.readline().strip()

        def set_value(key, value, ttl=0):
            flags = 16 if isinstance(value, str) else 1
            packed = value.encode() if flags == 16 else pickle.dumps(value, protocol=5)
            assert command(f"set {key} {flags} {ttl} {len(packed)}", packed + b"\r\n") == b"STORED"

        def read_value(key):
            first = command(f"get {key}")
            if first == b"END":
                return None
            name, returned, flags, length = first.split()
            assert (name, returned) == (b"VALUE", key.encode())
            value = cache.read(int(length))
            assert cache.read(7) == b"\r\nEND\r\n"
            if flags == b"16":
                return value.decode()
            assert flags == b"1"
            return pickle.loads(value)

        def delete(key):
            assert command(f"delete {key}") in (b"DELETED", b"NOT_FOUND")

        key = ":1:public-partnership-stats:v3"
        generation_key = f"{key}:generation"
        generation = uuid4()
        path = "/core/debaters/partnership-paths/stats/"
        results = Case(path + "results/", accept="text/html")
        sentinel = "<p>Shared partnership cache fixture</p>"
        set_value(generation_key, generation)
        fragment = f"{key}:{generation.hex}"
        set_value(fragment + ":lock", True, ttl=120)
        for server in (django, rust):
            assert request(server, results)[0] == 202
        delete(fragment + ":lock")
        set_value(fragment, sentinel, ttl=60)
        for server in (django, rust):
            assert request(server, results)[2] == sentinel.encode()

        driver.get(f"http://{HOST}:{rust.port}/robots.txt")
        driver.add_cookie({"name": "sessionid", "value": django_session()})
        driver.get(f"http://{HOST}:{rust.port}{path}")
        driver.find_element("css selector", f'form[action="{path}clear-cache/"] button').click()
        WebDriverWait(driver, 10).until(lambda browser: bool(browser.find_elements(
            "xpath", '//*[contains(@class,"alert") and contains(., "Partnership statistics cache cleared.")]')))
        replacement = read_value(generation_key)
        assert isinstance(replacement, UUID) and replacement != generation
        assert read_value(fragment) is None
        def ready(server):
            response = request(server, results)
            assert response[0] in (200, 202), response[0]
            return response if response[0] == 200 else False

        generated = WebDriverWait(driver, 30).until(lambda _: ready(rust))
        assert generated[2] != sentinel.encode()
        assert read_value(f"{key}:{replacement.hex}").encode() == generated[2]
        assert request(django, results)[2] == generated[2]

        next_generation = uuid4()
        set_value(generation_key, next_generation)
        generated = request(django, results)
        assert generated[0] == 200 and generated[2] != sentinel.encode()
        assert request(rust, results)[2] == generated[2]
        for current in (replacement, next_generation):
            delete(f"{key}:{current.hex}")
        delete(generation_key)
        driver.delete_cookie("sessionid")


def assert_search(driver, django, rust):
    paths = [
        "/search/",
        "/search/?q=Rory+Gillis",
        "/search/?q=%22New+York%22",
        "/search/?q=Harvard+-Ryan",
        "/search/?q=University&page=2",
        "/search/?q=alpha-betazzzzznope",
    ]
    for path in paths:
        reference = directory_state(driver, django, path)
        observed = directory_state(driver, rust, path)
        for key in reference:
            for index, (left, right) in enumerate(zip_longest(reference[key], observed[key])):
                assert left == right, f"{path} {key}[{index}] differed: {left!r} vs {right!r}"



def assert_search_reindex(rust):
    path = Path(os.environ["BR_STATE"]) / "dev.sqlite3"
    address = os.environ["BLACK_ROD_PARITY_MEMCACHED_ADDR"]
    query = Case("/search/?q=Uniqueparityindex", accept="text/html")
    label = b"Uniqueparityindex Debater"
    assert label not in request(rust, query)[2]
    subprocess.run([str(ROOT / "venv/bin/python"), "manage.py", "shell", "--verbosity", "0", "-c",
                    'from core.models import Debater; Debater.objects.create(first_name="Uniqueparityindex", last_name="Debater")'],
                   check=True, capture_output=True)
    assert label not in request(rust, query)[2]
    subprocess.run([str(RUST_BIN), "reindex_haystack", "--using", "default", "--remove-old"],
                   env={**os.environ, "BLACK_ROD_DATABASE_URL": f"sqlite://{path}?mode=ro",
                        "BLACK_ROD_MEMCACHED_ADDR": address}, check=True, capture_output=True)
    assert label in request(rust, query)[2]

def seed_browser_cases():
    code = """
from django import setup
setup()
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.sessions.backends.db import SessionStore
from core.models import AgentCredential
from core.models.debater import Debater
from core.models.immaculate_grid import ImmaculateGridBoard
from core.models.motion import Motion
from core.models.results.speaker import SpeakerResult
from core.models.results.team import TeamResult
from core.models.team import Team
from core.models.resource import Resource
from core.models.school import School
from core.models.school_admin import SchoolAdmin
from core.models.round import RoundStats
from core.models.tournament import Tournament
from core.models.video import Video
from core.utils.immaculate_grid.boards import game_date
import json
from datetime import timedelta
from django.utils import timezone

tournament = Tournament.objects.filter(team_results__isnull=False).distinct().order_by("-date", "pk").first()
debaters = list(Debater.objects.filter(temporary=False, synthetic=False).order_by("pk").values_list("pk", flat=True)[:4])
motion = Motion.objects.create(
    text="This House would use a disposable browser fixture.", background_slide="Fixture background",
    tournament=tournament, date_set=tournament.date,
)
motion.tags.add("Fixture topic")
award = SpeakerResult.objects.filter(tournament=tournament, type_of_place=1, place=1).order_by("pk").first()
result = {"tournament": tournament.pk, "debater": award.debater_id, "motion": motion.pk, "videos": [], "sessions": {},
    "authors": debaters, "users": {}, "role_school": School.objects.create(name="Disposable school role tests", short_name="Roles").pk}
from datetime import date
bundle_host = School.objects.get(pk=tournament.host_id)
bundle_event = Tournament.objects.create(host=bundle_host, season="2026", date=date(2026, 9, 15),
    manual_name="Disposable MIT-Tab bundle tournament")
original_award = TeamResult.objects.filter(tournament=tournament).first()
TeamResult.objects.create(tournament=bundle_event, team=original_award.team, type_of_place=1, place=1)
result["bundle_event"] = bundle_event.pk
result_school = School.objects.create(name="Disposable tournament scoring school", short_name="Result scoring")
manual = Tournament.objects.create(host=result_school, season="2026", date=date(2026, 9, 16),
    manual_name="Disposable results entry tournament", num_teams=24)
manual_people = [Debater.objects.create(first_name=name, last_name="Result", school=result_school)
    for name in ("First", "Second", "Third")]
manual_team = Team.objects.create(name="Disposable results team")
manual_team.debaters.add(*manual_people[:2])
result_award = TeamResult.objects.create(tournament=manual, team=manual_team, place=1)
result_award.hidden_from_debaters.add(manual_people[0])
SpeakerResult.objects.create(tournament=manual, debater=manual_people[2], place=1, hidden_from_profile=True)
result["manual_event"] = {"id":manual.pk,"people":[person.pk for person in manual_people],"team":manual_team.pk,
                         "school":result_school.pk}
result["historical_events"] = {}
for season in (2020, 2021):
    event = Tournament.objects.create(host=result_school, season=str(season), date=date(season, 9, 16),
        manual_name=f"Disposable online results {season}", qual_type=Tournament.YALE)
    result["historical_events"][str(season)] = event.pk
for access, _ in Video.PERMISSIONS:
    video = Video.objects.create(
        tournament=tournament, round=Video.VF, permissions=access,
        pm_id=debaters[0], mg_id=debaters[1], lo_id=debaters[2], mo_id=debaters[3],
        link=f"https://videos.example.test/access-{access}",
        password=f"fixture-password-{access}", case="<p>Fixture motion</p>",
        description="<p>Fixture description</p>",
    )
    video.tags.add("Rust parity test")
    result["videos"].append(video.pk)
result["managed_school"] = School.objects.create(name="Disposable managed school", short_name="Managed").pk
for label in ["ordinary", "account", "staff", "school_admin"]:
    user = get_user_model().objects.create_user(
        username=f"video-{label}", email=f"{label}@example.test", can_view_private_videos=label == "account",
    )
    if label == "staff":
        user.user_permissions.add(Permission.objects.get(content_type__app_label="core", codename="view_video"))
    if label == "school_admin":
        SchoolAdmin.objects.create(user=user, school_id=result["managed_school"], primary=True)
    session = SessionStore()
    session["_auth_user_id"] = str(user.pk)
    session["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
    session["_auth_user_hash"] = user.get_session_auth_hash()
    session.save()
    result["sessions"][label] = session.session_key
    result["users"][label] = user.pk
    if label == "ordinary":
        Debater.objects.filter(pk=debaters[0]).update(user=user)
result["resources"] = []
for index, (access, author) in enumerate([(0, debaters[1]), (1, debaters[1]), (1, debaters[0])]):
    record = Resource.objects.create(
        title=f"Resource browser fixture {index}", resource_type="case",
        content_link=f"https://resources.example.test/access-{index}", viewing_permission=access,
        usage_permissions="Fixture usage", description="Fixture description",
    )
    record.authors.add(author)
    record.tags.add("Fixture resource tag")
    result["resources"].append(record.pk)
grid_ids = list(Debater.objects.filter(alias_group_id__isnull=True).order_by("pk").values_list("pk", flat=True)[:10])
grid_day = game_date()
grid_rows = [{"key": f"r{i}", "label": f"Fixture row {i}"} for i in range(3)]
grid_columns = [{"key": f"c{i}", "label": f"Fixture column {i}"} for i in range(3)]
grid_aliases = {str(pk): f"d{pk}" for pk in grid_ids}
grid_scores = [{f"d{pk}": 40 + index} for index, pk in enumerate(grid_ids[:9])]
grid = ImmaculateGridBoard.objects.create(date=grid_day, rows=grid_rows, columns=grid_columns,
    answers=grid_scores, aliases=grid_aliases, clue_memberships={clue["key"]: list(grid_aliases.values())
        for clue in grid_rows + grid_columns})
result["grid"] = {"id": grid.pk, "day": grid_day.isoformat(), "debaters": grid_ids}
_, result["agent"] = AgentCredential.issue(
    admin=get_user_model().objects.get(username="tab"),
    name="Agent parity fixture",
    expires_at=timezone.now() + timedelta(days=1),
)
result["csv_debaters"] = list(RoundStats.objects.order_by("debater_id").values_list("debater_id", flat=True).distinct()[:3])
result["claim_candidates"] = list(Debater.objects.filter(
    user__isnull=True, school__isnull=False, school__temporary=False, school__synthetic=False
).exclude(pk__in=grid_ids).exclude(pk=award.debater_id).order_by("pk").values_list("pk", "school_id")[:2])
print(json.dumps(result))
"""
    result = subprocess.check_output([str(ROOT / "venv/bin/python"), "-c", code],
                                     env={**os.environ, "SECRET_KEY": "disposable-local-parity-key"}, text=True)
    return json.loads(result.splitlines()[-1])


def assert_video_permissions(driver, django, rust, fixture):
    paths = [f"/core/videos/{video}" for video in fixture["videos"]]
    users = [
        (None, [200, 302, 302]),
        (fixture["sessions"]["ordinary"], [200, 403, 403]),
        (fixture["sessions"]["account"], [200, 200, 403]),
        (fixture["sessions"]["staff"], [200, 200, 200]),
    ]
    for cookie, statuses in users:
        for access, (path, status) in enumerate(zip(paths, statuses)):
            for server in (django, rust):
                headers = {"Cookie": f"sessionid={cookie}"} if cookie else {}
                conn = server.connect()
                try:
                    conn.request("GET", path, headers=headers)
                    response = conn.getresponse()
                    body = unescape(response.read().decode())
                finally:
                    conn.close()
                password = f"fixture-password-{access}"
                link = f"https://videos.example.test/access-{access}"
                assert response.status == status, (server.name, access, status, response.status)
                assert (password in body) == (status == 200), (server.name, access, status, "password")
                assert (link in body) == (status == 200), (server.name, access, status, "link")
                if status == 302:
                    assert response.getheader("Location") == f"/auth/login?next={path}"
    for path in [paths[0], f"/core/tournaments/{fixture['tournament']}",
                 f"/core/debaters/{fixture['debater']}"]:
        reference = directory_state(driver, django, path)
        candidate = directory_state(driver, rust, path)
        for key in reference:
            for index, (left, right) in enumerate(zip_longest(reference[key], candidate[key])):
                assert left == right, f"{path} {key}[{index}] differed: {left!r} vs {right!r}"


def assert_api_basic_auth(django, rust, fixture):
    from base64 import b64encode
    from contextlib import closing

    code = """
from django import setup
setup()
from core.models import Debater, Round, Team, Tournament, Video
import sys

event = Tournament.objects.get(pk=sys.argv[1])
gov = Team.objects.get(pk=sys.argv[2])
opp = Team.objects.create(name="Basic API parity opponent")
opp.debaters.add(Debater.objects.get(pk=sys.argv[3]))
Round.objects.create(tournament=event, gov=gov, opp=opp, round_number=1, victor=Round.GOV)
speakers = dict(zip(("pm_id", "lo_id", "mg_id", "mo_id"), map(int, sys.argv[4:])))
Video.objects.create(tournament=event, round=Video.VF, permissions=Video.DEBATERS_IN_ROUND,
    link="https://videos.example.test/api-parity", password="api-parity-fixture", **speakers)
"""
    manual = fixture["manual_event"]
    subprocess.run([str(ROOT / "venv/bin/python"), "-c", code,
                    str(manual["id"]), str(manual["team"]), str(manual["people"][2]),
                    *map(str, fixture["authors"])], check=True)
    path = f"/api/tournaments/{manual['id']}/detail/"
    good = "Basic " + b64encode(b"tab:password").decode()
    bad = "Basic " + b64encode(b"tab:wrong-password").decode()
    cases = [
        Case(path), Case(path, authorization=good), Case(path, authorization=bad),
        Case(path, authorization="Basic"),
        Case(path, authorization="Basic invalid extra"),
        Case(path, authorization="Basic !!"),
        Case(path, method="OPTIONS", authorization=bad),
        Case("/api/schools/", authorization=bad),
    ]
    compare_responses(django, rust, cases)
    for server in (django, rust):
        authenticated = json.loads(request(server, Case(path, authorization=good))[2])
        assert authenticated["team_tab_cards"], server.name
        assert any(video["link"] == "https://videos.example.test/api-parity"
                   for video in authenticated["videos"]), server.name
        anonymous = json.loads(request(server, Case(path))[2])
        assert not anonymous["team_tab_cards"], server.name
        assert all(video["link"] != "https://videos.example.test/api-parity"
                   for video in anonymous["videos"]), server.name

        with closing(server.connect()) as connection:
            headers = {"Authorization": bad, "Cookie": f"sessionid={django_session()}"}
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            assert response.status == 200, server.name
            assert json.loads(response.read())["team_tab_cards"], server.name


def assert_video_editor(driver, django, rust, fixture):
    rust_root = f"http://{HOST}:{rust.port}"
    django_root = f"http://{HOST}:{django.port}"
    create = "/core/videos/create"
    driver.get(f"{rust_root}/games/")
    driver.add_cookie({"name": "sessionid", "value": django_session()})
    driver.get(f"{rust_root}{create}")
    wait = WebDriverWait(driver, 12)
    wait.until(lambda browser: len(browser.find_elements("css selector", ".note-editor .note-editable")) == 2)
    assert driver.find_element("id", "id_round").get_attribute("value") == "0"
    assert driver.find_element("id", "id_permissions").get_attribute("value") == "2"

    def write(root, path, data):
        driver.get(f"{root}{path}")
        return driver.execute_async_script("""
            const [data, done] = arguments;
            const fields = new URLSearchParams(data);
            fields.set("csrfmiddlewaretoken", document.querySelector("[name=csrfmiddlewaretoken]").value);
            fetch(location.pathname,{method:"POST",body:fields}).then(async response => {
                done([response.status,new URL(response.url).pathname,(await response.text()).includes("was introduced through the editor")]);
            }, () => done([0]));
        """, data)

    from base64 import b64encode
    from io import BytesIO
    pixels = BytesIO()
    Image.new("RGB", (2, 2), (53, 92, 127)).save(pixels, "PNG")
    uploaded = driver.execute_async_script("""
        const [data, done] = arguments;
        const bytes = Uint8Array.from(atob(data), char => char.charCodeAt(0));
        const body = new FormData();
        body.set("files", new File([bytes], "fixture.png"));
        body.set("csrfmiddlewaretoken", document.querySelector("[name=csrfmiddlewaretoken]").value);
        fetch("/summernote/upload_attachment/", {method:"POST",body}).then(async response => {
            done([response.status, await response.json()]);
        }, () => done([0]));
    """, b64encode(pixels.getvalue()).decode())
    assert uploaded[0] == 200 and uploaded[1]["files"][0]["size"] == len(pixels.getvalue()), uploaded
    attachment = uploaded[1]["files"][0]["url"]
    served = driver.execute_async_script("""
        const [path, done] = arguments;
        fetch(path).then(response => done([response.status,response.headers.get("content-type")]), () => done([0]));
    """, attachment)
    assert served == [200, "image/png"], served
    roles = dict(zip(("pm", "mg", "lo", "mo"), fixture["authors"]))
    data = {**roles, "tournament": fixture["tournament"], "round": "8", "permissions": "0",
            "link": "https://videos.example.test/new", "password": "",
            "case": '<p>This case was introduced through the editor.</p>',
            "description": f'<p>Test fixture.</p><img src="{attachment}">', "tags": "Video authoring fixture"}
    created = write(rust_root, create, data)
    assert created[0] == 200 and created[1].split("/")[-1].isdigit() and created[2], created
    view = created[1]
    driver.get(f"{django_root}{view}")
    assert "This case was introduced through the editor." in driver.find_element("tag name", "body").text
    data["round"] = "9"
    edited = write(rust_root, f"{view}/edit", data)
    assert edited == [200, view, True], edited
    driver.get(f"{django_root}{view}")
    assert "Varsity Finals" in driver.find_element("tag name", "body").text
    deleted = write(rust_root, f"{view}/delete", {})
    assert deleted[:2] == [200, "/core/videos/"], deleted
    driver.delete_all_cookies()


def assert_random_video(driver, django, rust, fixture):
    video = fixture["videos"][0]
    private_video = fixture["videos"][2]
    for server in (django, rust):
        root = f"http://{HOST}:{server.port}"
        driver.get(f"{root}/core/videos/random?id={video}")
        assert driver.current_url == f"{root}/core/videos/{video}"
        driver.get(f"{root}/core/videos/random?id={private_video}")
        assert driver.current_url == f"{root}/core/videos/?id={private_video}"
        warning = driver.find_element("css selector", ".alert-warning").text
        assert warning == "No results found, or you don't have access to any matching videos."
        driver.refresh()
        assert not driver.find_elements("css selector", ".alert-warning")


def assert_recent_results(driver, django, rust):
    cards = []
    for server in (django, rust):
        path = "/core/widgets/recent-results/"
        conn = server.connect()
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            assert response.status == 200 and response.getheader("X-Frame-Options") is None
            response.read()
        finally:
            conn.close()
        driver.get(f"http://{HOST}:{server.port}{path}")
        cards.append(driver.execute_script("""
            return Array.from(document.querySelectorAll("article.card")).map(card => [
                card.textContent.replace(/\s+/g, " ").trim(),
                Array.from(card.querySelectorAll("a")).map(node => node.getAttribute("href")),
            ]);
        """))
    assert cards[0] and cards[0] == cards[1], f"Recent results differed: {cards[0]!r} vs {cards[1]!r}"


def assert_motion_progress(driver, django, rust, fixture):
    path = f"/core/motions/{fixture['motion']}"
    label = "This House would use a disposable browser fixture."
    for server in (django, rust):
        driver.get(f"http://{HOST}:{server.port}{path}")
        assert driver.find_element("css selector", "p.lead").text == label
        assert not driver.find_elements("css selector", "button[value=done]")
    for server in (django, rust):
        root = f"http://{HOST}:{server.port}"
        driver.get(f"{root}/core/motions/random?text__icontains=disposable")
        assert driver.current_url == f"{root}{path}"
        driver.get(f"{root}/core/motions/random?text__icontains=zzz-nonexistent")
        assert driver.current_url == f"{root}/core/motions/?text__icontains=zzz-nonexistent&progress=exclude_done_ignore"
        assert driver.find_element("css selector", ".alert-info").text == "No motions match those filters."
    driver.add_cookie({"name": "sessionid", "value": fixture["sessions"]["ordinary"]})
    for writer, reader, target, active in [(rust, django, "done", "btn-success"),
                                            (django, rust, "ignore", "btn-secondary"),
                                            (rust, django, "clear", None)]:
        driver.get(f"http://{HOST}:{writer.port}{path}")
        selector = f"button[name=status][value={target}]"
        button = driver.find_element("css selector", selector)
        driver.execute_script("arguments[0].form.requestSubmit(arguments[0])", button)
        WebDriverWait(driver, 10).until(lambda browser: (
            browser.current_url == f"http://{HOST}:{writer.port}{path}" and
            (active in browser.find_element("css selector", selector).get_attribute("class")
             if active else not browser.find_elements("css selector", selector))
        ))
        driver.get(f"http://{HOST}:{reader.port}{path}")
        if active:
            assert active in driver.find_element("css selector", selector).get_attribute("class")
        else:
            assert not driver.find_elements("css selector", selector)
        for app in (writer, reader):
            root = f"http://{HOST}:{app.port}"
            filter_path = "/core/motions/?text__icontains=disposable"
            driver.get(f"{root}{filter_path}")
            assert bool(driver.find_elements("css selector", f"a[href='{path}']")) == (target == "clear")
            if target != "clear":
                driver.get(f"{root}{filter_path}&progress={target}")
                assert driver.find_element("css selector", f"a[href='{path}']").text == label
    driver.delete_cookie("sessionid")


def assert_motion_editor(driver, django, rust, fixture):
    rust_root = f"http://{HOST}:{rust.port}"
    django_root = f"http://{HOST}:{django.port}"
    cookie = django_session()
    path = "/core/motions/create"
    driver.get(f"{rust_root}{path}")
    assert urlsplit(driver.current_url).path == "/auth/login/"
    driver.add_cookie({"name": "sessionid", "value": fixture["sessions"]["ordinary"]})
    driver.get(f"{rust_root}{path}")
    assert not driver.find_elements("id", "id_text")
    driver.get(f"{rust_root}/games/")
    driver.add_cookie({"name": "sessionid", "value": cookie})
    driver.get(f"{rust_root}{path}")
    wait = WebDriverWait(driver, 10)
    wait.until(lambda browser: browser.find_element("id", "id_tags").get_attribute("class")
               .find("select2-hidden-accessible") >= 0)

    def save(root, endpoint, values):
        driver.get(f"{root}{endpoint}")
        return driver.execute_async_script("""
            const [values, done] = arguments;
            const data = new URLSearchParams(values);
            data.set("csrfmiddlewaretoken", document.querySelector("[name=csrfmiddlewaretoken]").value);
            fetch(location.pathname, {method: "POST", body: data}).then(async response => {
                const html = await response.text();
                const body = new DOMParser().parseFromString(html, "text/html");
                done({status: response.status, path: new URL(response.url).pathname, text: body.body.textContent});
            }, () => done({status: 0}));
        """, values)

    initial = {
        "text": "This House would compare motion authoring.",
        "background_slide": "Initial background",
        "date_set": "2024-01-14",
        "tournament": fixture["tournament"],
        "tags": '"Motion authoring fixture"',
    }
    created = save(rust_root, path, initial)
    assert created["status"] == 200 and created["path"] != path, created
    detail = created["path"]
    driver.get(f"{django_root}{detail}")
    assert driver.find_element("css selector", "p.lead").text == initial["text"]
    duplicate = save(rust_root, path, initial)
    assert duplicate["path"] == path and "Motion with this Motion text already exists." in duplicate["text"]
    edited = {**initial, "background_slide": "Updated background", "tags": "changed"}
    updated = save(django_root, f"{detail}/edit", edited)
    assert updated["path"] == detail and updated["status"] == 200
    driver.get(f"{rust_root}{detail}/edit")
    assert driver.find_element("id", "id_background_slide").get_attribute("value") == "Updated background"
    assert driver.find_element("css selector", "#id_tags option[selected]").text == "changed"
    driver.delete_all_cookies()


def assert_motion_import(driver, django, rust, fixture):
    roots = [f"http://{HOST}:{server.port}" for server in (django, rust)]
    path = "/core/motions/import"
    driver.get(f"{roots[1]}/games/")
    driver.add_cookie({"name": "sessionid", "value": django_session()})

    def preview(root, filename, content):
        driver.get(f"{root}{path}")
        return driver.execute_async_script("""
            const [name, content, done] = arguments;
            const data = new FormData(document.querySelector("form[enctype='multipart/form-data']"));
            const bytes = Uint8Array.from(atob(content), char => char.charCodeAt(0));
            data.set("spreadsheet", new File([bytes], name));
            fetch(location.pathname, {method:"POST", body:data}).then(async response => {
                const document = new DOMParser().parseFromString(await response.text(), "text/html");
                done({status: response.status, token: document.querySelector("[name=rows]")?.value,
                    rows: Array.from(document.querySelectorAll("tbody tr")).map(row =>
                        Array.from(row.cells).map(cell => cell.textContent.trim()))});
            }, () => done({status:0}));
        """, filename, content)

    def confirm(root, token):
        driver.get(f"{root}{path}")
        return driver.execute_async_script("""
            const [token, done] = arguments;
            const body = new URLSearchParams({confirm:"1", rows:token, csrfmiddlewaretoken:
                document.querySelector("form[enctype='multipart/form-data'] [name=csrfmiddlewaretoken]").value});
            fetch(location.pathname, {method:"POST", body}).then(async response => {
                done({status:response.status, redirected:response.redirected, text:await response.text()});
            }, () => done({status:0}));
        """, token)

    from base64 import b64encode
    csv = ('Motion,Date,Tournament,Topics\n'
           f'"This House would import the fixture CSV.",2/14/2026,{fixture["tournament"]},"Ethics; Finance"\n'
           'This is invalid,not-a-date,999999,\n'
           'This House would use a disposable browser fixture.,,,\n')
    xlsx = subprocess.check_output([str(ROOT / "venv/bin/python"), "-c", """
from base64 import b64encode
from datetime import date
from io import BytesIO
from openpyxl import Workbook
from sys import argv
book=Workbook()
book.active.append(["A non-selected worksheet"])
sheet=book.create_sheet("Motion import")
book.active=sheet
sheet.append(["Motion", "Date", "Tournament", "Topics"])
sheet.append(["This House would import the fixture XLSX.", date(2026, 2, 14), int(argv[1]), "Ethics; Finance"])
out=BytesIO()
book.save(out)
print(b64encode(out.getvalue()).decode())
""", str(fixture["tournament"])], text=True).strip()
    for name, content, confirmer in (("motions.csv", b64encode(csv.encode()).decode(), 0),
                                     ("motions.xlsx", xlsx, 1)):
        expected = preview(roots[0], name, content)
        actual = preview(roots[1], name, content)
        assert expected["status"] == actual["status"] == 200 and expected["rows"] == actual["rows"], (expected, actual)
        assert actual["rows"][0][2] == "2026-02-14"
        rejected = confirm(roots[1], actual["token"] + "invalid")
        assert rejected["status"] == 400 and not rejected["redirected"], rejected
        saved = confirm(roots[confirmer], (actual if confirmer == 0 else expected)["token"])
        assert saved["status"] == 200 and saved["redirected"] and "Imported 1 motions" in saved["text"], saved
    driver.delete_all_cookies()


def assert_resource_permissions(driver, django, rust, fixture):
    users = [
        (None, [200, 302, 302]),
        (fixture["sessions"]["ordinary"], [200, 403, 200]),
        (fixture["sessions"]["account"], [200, 200, 200]),
    ]
    for cookie, statuses in users:
        for server in (django, rust):
            root = f"http://{HOST}:{server.port}"
            driver.get(f"{root}/core/resources/")
            if cookie:
                driver.add_cookie({"name": "sessionid", "value": cookie})
            else:
                driver.delete_cookie("sessionid")
            driver.get(f"{root}/core/resources/?title__icontains=browser")
            for index, (pk, status) in enumerate(zip(fixture["resources"], statuses)):
                path = f"/core/resources/{pk}"
                assert bool(driver.find_elements("css selector", f"a[href='{path}']")) == (status == 200)
                conn = server.connect()
                try:
                    conn.request("GET", path, headers={"Cookie": f"sessionid={cookie}"} if cookie else {})
                    response = conn.getresponse()
                    body = unescape(response.read().decode())
                    assert response.status == status, (server.name, path, response.status)
                    assert (f"https://resources.example.test/access-{index}" in body) == (status == 200)
                finally:
                    conn.close()
    driver.delete_cookie("sessionid")
    public = f"/core/resources/{fixture['resources'][0]}"
    reference = directory_state(driver, django, public)
    candidate = directory_state(driver, rust, public)
    assert reference == candidate, f"Resource detail differed: {reference!r} vs {candidate!r}"


def assert_resource_editor(driver, django, rust, fixture):
    roots = [f"http://{HOST}:{server.port}" for server in (django, rust)]
    create = "/core/resources/create"
    driver.get(f"{roots[1]}/games/")
    driver.add_cookie({"name": "sessionid", "value": django_session()})

    def submit(root, path, pairs):
        driver.get(f"{root}{path}")
        return driver.execute_async_script("""
            const [pairs, done] = arguments;
            const form = new URLSearchParams(pairs);
            form.set("csrfmiddlewaretoken", document.querySelector("[name=csrfmiddlewaretoken]").value);
            fetch(location.pathname, {method: "POST", body: form}).then(async response => {
                const html = new DOMParser().parseFromString(await response.text(), "text/html");
                done({status: response.status, path: new URL(response.url).pathname, text: html.body.textContent});
            }, () => done({status: 0}));
        """, pairs)

    for root in roots:
        driver.get(f"{root}{create}")
        assert driver.find_element("id", "id_resource_type").get_attribute("value") == "other"
        assert driver.find_element("id", "id_viewing_permission").get_attribute("value") == "0"
    fields = [
        ["title", "Parity resource created in Rust"], ["resource_type", "writeup"],
        ["authors", fixture["authors"][0]], ["authors", fixture["authors"][1]],
        ["content_link", "https://resources.example.test/rust"], ["description", "A draft with two authors."],
        ["usage_permissions", "Please credit the authors."], ["viewing_permission", "0"],
        ["tags", "Fixture resource tag"], ["tags", "Second fixture resource tag"],
    ]
    invalid = submit(roots[1], create, [pair for pair in fields if pair[0] != "authors"])
    assert invalid["path"] == create and "This field is required." in invalid["text"], invalid
    created = submit(roots[1], create, fields)
    assert created["status"] == 200 and created["path"].startswith("/core/resources/"), created
    detail = created["path"]
    assert detail.split("/")[-1].isdigit(), created
    edit = f"{detail}/edit"
    driver.get(f"{roots[0]}{edit}")
    assert driver.find_element("id", "id_title").get_attribute("value") == fields[0][1]
    assert driver.find_element("id", "id_resource_type").get_attribute("value") == "writeup"
    fields[0][1] = "Parity resource edited in Rust"
    fields = [pair for pair in fields if pair != ["tags", "Fixture resource tag"]]
    updated = submit(roots[1], edit, fields)
    assert updated["path"] == detail and updated["status"] == 200 and fields[0][1] in updated["text"], updated
    driver.get(f"{roots[0]}{detail}")
    assert "Second fixture resource tag" in driver.find_element("tag name", "body").text
    deleted = submit(roots[1], f"{detail}/delete", [])
    assert deleted["status"] == 200 and deleted["path"] == "/core/resources/", deleted
    driver.get(f"{roots[0]}{create}")
    missing = driver.execute_async_script("""
        const [path, done] = arguments;
        fetch(path).then(response => done(response.status), () => done(0));
    """, detail)
    assert missing == 404, missing
    driver.delete_all_cookies()


def assert_tag_creation(driver, django, rust, fixture):
    tab = django_session()
    path = f"/core/motions/{fixture['motion']}"
    driver.get(f"http://{HOST}:{rust.port}{path}")
    driver.add_cookie({"name": "sessionid", "value": tab})
    driver.refresh()
    token = driver.find_element("name", "csrfmiddlewaretoken").get_attribute("value")
    csrf = driver.get_cookie("csrftoken")["value"]
    cases = [
        ("/core/videos/tags", "Video browser parity"),
        ("/core/resources/tags", "Resource browser parity"),
        ("/core/motions/topics", "École browser parity"),
        ("/core/motions/topics", "Ecole browser parity"),
    ]
    for index, (route, name) in enumerate(cases):
        values = urlencode({"text": name})
        bodies = []
        writers = (rust, django) if index % 2 == 0 else (django, rust)
        for server in writers:
            conn = server.connect()
            try:
                conn.request("POST", route, values, {
                    "Cookie": f"sessionid={tab}; csrftoken={csrf}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-CSRFToken": token,
                })
                response = conn.getresponse()
                assert response.status == 200, (server.name, route, response.status)
                bodies.append(json.loads(response.read()))
            finally:
                conn.close()
        assert bodies[0] == bodies[1] and bodies[0]["text"] == name
    driver.delete_all_cookies()


def assert_grid(driver, django, rust, fixture):
    play = fixture["grid"]
    answers = f"/immaculate-grid/answers/?day={play['day']}&board={play['id']}&cell=0"
    cookie = fixture["sessions"]["ordinary"]
    driver.get(f"http://{HOST}:{rust.port}/immaculate-grid/")
    assert urlsplit(driver.current_url).path == "/auth/login/"
    driver.add_cookie({"name": "sessionid", "value": cookie})
    driver.get(f"http://{HOST}:{rust.port}/immaculate-grid/")
    assert len(driver.find_elements("css selector", "[data-cell]")) == 9
    csrf = driver.get_cookie("csrftoken")["value"]
    token = driver.find_element("name", "csrfmiddlewaretoken").get_attribute("value")

    def fetch(server, path, values=None):
        conn = server.connect()
        try:
            headers = {"Cookie": f"csrftoken={csrf}; sessionid={cookie}"}
            if values is not None:
                headers.update({"Content-Type": "application/x-www-form-urlencoded", "X-CSRFToken": token})
            conn.request("POST" if values is not None else "GET", path,
                         urlencode(values) if values is not None else None, headers)
            response = conn.getresponse()
            return response.status, response.read().decode(), response.getheader("Location")
        finally:
            conn.close()

    for server in (django, rust):
        status, body, _ = fetch(server, answers)
        assert status == 403 and json.loads(body)["error"] == "Solve all nine squares to view valid answers."
    for query in ["q=Th", "q=Rory+Gillis", "q=a", "q=%25"]:
        reference = fetch(django, f"/immaculate-grid/search/?{query}")
        candidate = fetch(rust, f"/immaculate-grid/search/?{query}")
        assert reference[0] == candidate[0] == 200
        assert json.loads(reference[1]) == json.loads(candidate[1])

    guess = "/immaculate-grid/guess/"
    wrong = {"day": play["day"], "board": play["id"], "attempts": 0, "cell": 0, "debater": play["debaters"][-1]}
    with ThreadPoolExecutor(max_workers=2) as workers:
        responses = list(workers.map(lambda _: fetch(rust, guess, wrong), range(2)))
    assert all(status == 302 and location == "/immaculate-grid/" for status, _, location in responses)
    for cell, debater in enumerate(play["debaters"][:9]):
        writer = django if cell % 2 == 0 else rust
        status, _, location = fetch(writer, guess, {**wrong, "cell": cell, "attempts": cell + 1, "debater": debater})
        assert status == 302 and location == "/immaculate-grid/", (writer.name, cell, status)
    reveals = []
    share = []
    for server in (django, rust):
        status, body, _ = fetch(server, answers)
        assert status == 200, (server.name, status, body)
        reveals.append(json.loads(body))
        driver.get(f"http://{HOST}:{server.port}/immaculate-grid/")
        assert len(driver.find_elements("css selector", ".immaculate-correct")) == 9
        assert not driver.find_elements("css selector", "[data-cell]")
        value = driver.find_element("id", "immaculate-share-results").get_attribute("value")
        share.append(value.replace(f"http://{HOST}:{server.port}", "http://fixture"))
    assert reveals[0] == reveals[1] and reveals[1]["answers"][0]["score"] == 40
    assert share[0] == share[1] and "9/9 solved · 391 points" in share[0], repr(share)
    reference = directory_state(driver, django, "/immaculate-grid/leaderboard/")
    candidate = directory_state(driver, rust, "/immaculate-grid/leaderboard/")
    assert reference == candidate, f"Grid leaderboard differed: {reference!r} vs {candidate!r}"
    driver.delete_all_cookies()


def assert_grid_generator(driver, django, rust, fixture):
    code = """
from django import setup
setup()
from core.models import ImmaculateGridBoard, ImmaculateGridPlay
from core.utils.immaculate_grid.boards import build_board, game_date
from core.utils.immaculate_grid.catalog import load_catalog
import json
import sys

identifier = int(sys.stdin.read())
test_board = ImmaculateGridBoard.objects.get(pk=identifier)
assert test_board.rows[0]["label"] == "Fixture row 0"
ImmaculateGridPlay.objects.filter(board=test_board).delete()
test_board.delete()
day = game_date()
reference = build_board(day, load_catalog(day), ImmaculateGridBoard.objects.exclude(date=day))
keys = ["rows", "columns", "answers", "aliases", "clue_memberships"]
print(json.dumps({key: getattr(reference, key) for key in keys}))
"""
    result = subprocess.check_output([str(ROOT / "venv/bin/python"), "-c", code],
                                     input=str(fixture["grid"]["id"]), text=True)
    expected = json.loads(result.splitlines()[-1])
    path = "/immaculate-grid/"
    driver.get(f"http://{HOST}:{rust.port}{path}")
    driver.add_cookie({"name": "sessionid", "value": fixture["sessions"]["ordinary"]})
    driver.get(f"http://{HOST}:{rust.port}{path}")
    assert len(driver.find_elements("css selector", "[data-cell]")) == 9
    database = Path(os.environ["BR_STATE"]) / "dev.sqlite3"
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as conn:
        candidate = conn.execute(
            "SELECT rows, columns, answers, aliases, clue_memberships FROM core_immaculategridboard"
            " WHERE date = ? AND replaced_at IS NULL", (fixture["grid"]["day"],),
        ).fetchone()
    for name, data in zip(expected, candidate):
        found = json.loads(data)
        details = ""
        if name in ["rows", "columns"]:
            details = f" {expected[name]!r} vs {found!r}"
        assert found == expected[name], f"Generated grid {name} differed.{details}"
    driver.get(f"http://{HOST}:{django.port}{path}")
    assert len(driver.find_elements("css selector", "[data-cell]")) == 9
    driver.delete_all_cookies()


def assert_infinite_grid(driver, django, rust, fixture):
    path = "/immaculate-grid/infinite/"
    cookie = fixture["sessions"]["ordinary"]
    rust_root = f"http://{HOST}:{rust.port}"
    django_root = f"http://{HOST}:{django.port}"
    driver.get(f"{rust_root}{path}")
    driver.add_cookie({"name": "sessionid", "value": cookie})
    driver.get(f"{rust_root}{path}")
    assert not driver.find_elements("css selector", "[data-cell]")
    form = driver.find_element("css selector", f"form[action='{path}']")
    driver.execute_script("arguments[0].requestSubmit()", form)
    wait = WebDriverWait(driver, 15)
    wait.until(lambda browser: len(browser.find_elements("css selector", "[data-cell]")) == 9)
    practice = driver.find_element("name", "board").get_attribute("value")
    assert int(practice) > 2**64
    driver.get(f"{django_root}{path}")
    assert driver.find_element("name", "board").get_attribute("value") == practice

    code = """
from django import setup
setup()
from django.contrib.sessions.backends.db import SessionStore
import json
import sys
play = SessionStore(sys.stdin.read()).load()["immaculate_grid_practice"]
board = play["board"]
people = set()
picks = []
for index, answers in enumerate(board["answers"][:2]):
    person = next(person for person in answers if person not in people)
    people.add(person)
    profile = next(pk for pk, link in board["aliases"].items() if link == person)
    picks.append({"cell": index, "debater": profile, "day": board["date"], "board": str(board["id"])})
print(json.dumps(picks))
"""
    result = subprocess.check_output([str(ROOT / "venv/bin/python"), "-c", code], input=cookie,
                                     env={**os.environ, "SECRET_KEY": "disposable-local-parity-key"}, text=True)
    for attempts, (values, writer) in enumerate(zip(json.loads(result.splitlines()[-1]), (django_root, rust_root))):
        driver.get(f"{writer}{path}")
        values["attempts"] = attempts
        response = driver.execute_async_script("""
            const [values, done] = arguments;
            const data = new URLSearchParams(values);
            data.set("csrfmiddlewaretoken", document.querySelector("[name=csrfmiddlewaretoken]").value);
            fetch("/immaculate-grid/infinite/guess/", {method: "POST", body: data})
                .then(response => done(response.status), () => done(0));
        """, values)
        assert response == 200, (writer, response)
    for runtime in (django_root, rust_root):
        driver.get(f"{runtime}{path}")
        assert len(driver.find_elements("css selector", ".immaculate-correct")) == 2
        assert driver.find_element("name", "board").get_attribute("value") == practice
    driver.delete_all_cookies()


def assert_career_export(django, rust, fixture):
    cookie = django_session()
    access = fixture["sessions"]["ordinary"]
    for pk in fixture["csv_debaters"]:
        path = f"/core/debaters/{pk}/tab-cards-csv"
        bodies = []
        for server in (django, rust):
            for token, expected in [(None, 302), (access, 404), (cookie, 200)]:
                headers = {"Cookie": f"sessionid={token}"} if token else {}
                conn = server.connect()
                try:
                    conn.request("GET", path, headers=headers)
                    response = conn.getresponse()
                    payload = response.read()
                    assert response.status == expected, (server.name, path, expected, response.status)
                    if expected == 200:
                        assert response.getheader("Content-Type") == "text/csv"
                        bodies.append((response.getheader("Content-Disposition"), payload))
                finally:
                    conn.close()
        assert bodies[0] == bodies[1], f"Career CSV {pk} differed: {bodies[0]!r} vs {bodies[1]!r}"
        assert bodies[0][1].count(b"\r\n") > 1


def assert_claims(driver, django, rust, fixture):
    django_root = f"http://{HOST}:{django.port}"
    rust_root = f"http://{HOST}:{rust.port}"
    request = "/core/claim-debater/request/"
    profile = "/core/my-profile/"
    review = "/core/claim-debater/review/"
    cookie = fixture["sessions"]["ordinary"]
    staff = django_session()

    def login(token):
        driver.get(f"{rust_root}/games/")
        driver.add_cookie({"name": "sessionid", "value": token})

    def post(root, path, values):
        driver.get(f"{root}{path}")
        return driver.execute_async_script("""
            const [values, done] = arguments;
            const data = new URLSearchParams(values);
            data.set("csrfmiddlewaretoken", document.querySelector("[name=csrfmiddlewaretoken]").value);
            fetch(location.pathname, {method: "POST", body: data}).then(async response => {
                const html = await response.text();
                const parsed = new DOMParser().parseFromString(html, "text/html");
                done({status: response.status, path: new URL(response.url).pathname, text: parsed.body.textContent});
            }, () => done({status: 0}));
        """, values)

    for index, (debater, school) in enumerate(fixture["claim_candidates"]):
        login(cookie)
        creator = rust_root if index == 0 else django_root
        result = post(creator, request, {"school": school, "debater": debater})
        assert result["path"] == profile and result["status"] == 200, result
        assert "Claim request submitted." in result["text"], result
        login(staff)
        reviewer = rust_root if index == 0 else django_root
        driver.get(f"{reviewer}{review}")
        rows = driver.find_elements("css selector", f'tr:has(a[href="/core/debaters/{debater}"]) input[name=request_id]')
        assert rows, (debater, driver.find_element("tag name", "body").text[:400])
        identifier = rows[0].get_attribute("value")
        action = "approve" if index == 0 else "deny"
        result = post(reviewer, review, {"request_id": identifier, "action": action, "denial_reason": "Fixture denial"})
        assert result["path"] == review and result["status"] == 200, result
        assert ("Approved claim request" if index == 0 else "Claim request denied.") in result["text"]
        login(cookie)
        left = directory_state(driver, django, profile)
        right = directory_state(driver, rust, profile)
        assert left == right, (left, right)
        if index == 0:
            edit = f"/core/debater/{debater}/edit-profile/"
            result = post(rust_root, edit, {
                "first_name": "Parity", "last_name": "Profile", "email": "profile@example.test",
                "first_season": "2024", "latest_season": "2025", "status": "2",
                "paradigm": "https://docs.google.com/document/d/fixture",
                "elo_manual_opt": "opt_out", "dino_to_contact_opt_in": "on",
                "dino_judge_contact_opt_in": "on", "region": "west",
            })
            assert result["status"] == 200 and result["path"] == f"/core/debaters/{debater}", result
            driver.get(f"{django_root}{edit}")
            assert driver.find_element("id", "id_last_name").get_attribute("value") == "Profile"
            assert driver.find_element("id", "id_dino_judge_contact_opt_in").is_selected()
            assert driver.find_element("css selector", "#id_region option[value=west]").is_selected()
            for path in ("/core/dino-judges/?first_name__icontains=Parity&region=west",
                         "/core/dino-tos/?first_name__icontains=Parity"):
                left = directory_state(driver, django, path)
                right = directory_state(driver, rust, path)
                assert left == right, (path, left, right)
                assert any(row[1:3] == ["Parity", "Profile"] for row in left["rows"]), (path, left)
    driver.delete_all_cookies()


def assert_scheduling(driver, django, rust, fixture):
    path = "/core/admin/scheduling/"
    root = [f"http://{HOST}:{server.port}" for server in (django, rust)]
    driver.get(f"{root[1]}/games/")
    driver.add_cookie({"name": "sessionid", "value": fixture["sessions"]["ordinary"]})
    status = driver.execute_async_script("""
        const [url, done] = arguments;
        fetch(url).then(response => done(response.status), () => done(0));
    """, path)
    assert status == 403, status
    driver.add_cookie({"name": "sessionid", "value": django_session()})
    driver.get(f"{root[1]}{path}")
    assert "Scheduling Workspace" in driver.find_element("tag name", "body").text, driver.page_source[:300]

    def call(server, endpoint, method=None, body=None):
        driver.get(f"{server}{path}")
        return driver.execute_async_script("""
            const [endpoint, method, body, done] = arguments;
            const options = method ? {method, headers: {
                "X-CSRFToken": document.querySelector("[name=csrfmiddlewaretoken]").value
            }} : {};
            if (method === "POST" && endpoint === "") {
                const form = new FormData();
                form.set("action", "upload_csv");
                form.set("workspace_version", document.querySelector("[name=workspace_version]").value);
                for (const [name, content] of Object.entries(body)) {
                    form.set(name + "_csv", new File([content], name + ".csv"));
                }
                options.body = form;
            } else if (method === "POST") {
                options.headers["Content-Type"] = "application/json";
                options.body = JSON.stringify(body);
            }
            fetch(location.pathname + endpoint, options).then(async response => {
                const text = await response.text();
                const contentType = response.headers.get("content-type") || "";
                done([response.status, contentType.includes("application/json") ? JSON.parse(text) : text]);
            }, () => done([0]));
        """, endpoint, method, body)

    schools = ',,,,,9/8,1/6\n,# of Tournaments,,,,,\nSchool Name,Region,Desired,Priority,Tags,,,,\n"University, College",North,1,2,"Novice, Open",Rank 1,Rank 3\n'
    dates = 'Date,Count,Tags\n9/8,1-2,"Unopposed,Novice"\n1/6,0,\n'
    status, html = call(root[1], "", "POST", {"schools": schools, "dates": dates})
    assert status == 200 and "University, College" in html, (status, html[:200])
    expected, actual = [call(server, "workspace-data/") for server in root]
    assert expected == actual, (expected, actual)
    assert actual[0] == 200 and actual[1]["workspace"]["summary"]["scenario_count"] == 2, actual
    workspace = actual[1]["workspace"]
    run = {"workspace_version": workspace["version"] - 1, "status": "completed"}
    expected, actual = [call(server, "save-browser-run/", "POST", run) for server in root]
    assert expected == actual and actual[0] == 409, (expected, actual)
    run["workspace_version"] = workspace["version"]
    run["output_text"] = "Fixture scheduling result"
    run["result"] = {"best_penalty": 100, "best_seed": 7}
    status, saved = call(root[1], "save-browser-run/", "POST", run)
    assert status == 200, (status, saved)
    for server in root:
        assert call(server, f"runs/{saved['run_id']}/output/") == [200, run["output_text"]]
        assert call(server, "download/schools/") == [200, schools]
    driver.delete_all_cookies()


def assert_entities(driver, django, rust, fixture):
    rust_root = f"http://{HOST}:{rust.port}"
    django_root = f"http://{HOST}:{django.port}"
    driver.get(f"{rust_root}/games/")
    driver.add_cookie({"name": "sessionid", "value": django_session()})

    def submit(root, path, fields):
        driver.get(f"{root}{path}")
        return driver.execute_async_script("""
            const [fields, done] = arguments;
            const body = new URLSearchParams(fields);
            const token = document.querySelector("[name=csrfmiddlewaretoken]");
            if (!token) return done(["missing form", document.body.textContent.slice(0, 200)]);
            body.set("csrfmiddlewaretoken", token.value);
            fetch(location.pathname, {method:"POST",body}).then(async response => {
                done([response.status,new URL(response.url).pathname,await response.text()]);
            }, () => done([0]));
        """, fields)

    school_name = {"name": "Parity School Created", "short_name": "Parity", "included_in_oty": "on"}
    created_school = submit(rust_root, "/core/schools/create", school_name)
    assert created_school[0] == 200 and created_school[1].split("/")[-1].isdigit(), created_school[:2]
    school = created_school[1]
    school_id = school.split("/")[-1]
    driver.get(f"{django_root}{school}/edit")
    assert driver.find_element("id", "id_name").get_attribute("value") == school_name["name"]
    school_name["name"] = "Parity School Edited"
    assert submit(rust_root, f"{school}/edit", school_name)[:2] == [200, school]
    driver.get(f"{django_root}{school}/edit")
    assert driver.find_element("id", "id_name").get_attribute("value") == school_name["name"]
    participant = {"first_name": "Native", "last_name": "Created", "school": school_id, "ajax": "1"}
    created_debater = submit(rust_root, "/core/debaters/create", participant)
    assert created_debater[0] == 200 and created_debater[2].isdigit(), created_debater[:2]
    debater = f"/core/debaters/{created_debater[2]}"
    driver.get(f"{django_root}{debater}/edit")
    assert driver.find_element("id", "id_last_name").get_attribute("value") == "Created"
    participant.pop("ajax")
    participant["last_name"] = "Updated"
    assert submit(rust_root, f"{debater}/edit", participant)[:2] == [200, debater]
    driver.get(f"{django_root}{debater}/edit")
    assert driver.find_element("id", "id_last_name").get_attribute("value") == "Updated"
    assert submit(rust_root, f"{school}/delete", {})[:2] == [200, "/core/schools/"]
    database = Path(os.environ["BR_STATE"]) / "dev.sqlite3"
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT school_id FROM core_debater WHERE id = ?",
                                  (created_debater[2],)).fetchone() == (None,)
    assert submit(rust_root, f"{debater}/delete", {})[:2] == [200, "/core/debaters/"]
    data = {"host": fixture["claim_candidates"][0][1], "season": "2026", "date": "2026-09-01",
            "num_teams": "24", "num_novice_debaters": "12", "qual_type": "15",
            "name_suffix": "0", "manual_name": "Disposable parity event", "short_name": ""}
    created_event = submit(rust_root, "/core/tournaments/create", data)
    assert created_event[0] == 200 and created_event[1].split("/")[-1].isdigit(), created_event[:2]
    tournament = created_event[1]
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
        row = connection.execute("SELECT qual,noty,toty,soty,autoqual_bar FROM core_tournament WHERE id=?",
                                 (tournament.split("/")[-1],)).fetchone()
        assert row == (True, False, False, False, 2), row
    driver.get(f"{django_root}{tournament}/edit")
    assert driver.find_element("id", "id_manual_name").get_attribute("value") == data["manual_name"]
    data["manual_name"] = "Edited parity event"
    edited = submit(rust_root, f"{tournament}/edit", data)
    assert edited[:2] == [200, tournament] and data["manual_name"] in edited[2], edited[:2]
    assert submit(rust_root, f"{tournament}/delete", {})[:2] == [200, "/core/tournaments/"]
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
        team_id = connection.execute("""
            SELECT t.id FROM core_team t JOIN core_team_debaters m ON m.team_id=t.id
            JOIN core_debater d ON d.id=m.debater_id
            WHERE NOT t.synthetic AND NOT d.synthetic AND NOT d.temporary
            GROUP BY t.id HAVING COUNT(*)=2 ORDER BY t.id LIMIT 1
        """).fetchone()[0]
        member_ids = [row[0] for row in connection.execute(
            "SELECT debater_id FROM core_team_debaters WHERE team_id=?",
            (team_id,)).fetchall()]
    team = f"/core/teams/{team_id}"
    fields = [["short_name", "Parity team name"]]
    invalid = submit(rust_root, f"{team}/edit", fields + [["debaters", member_ids[0]]])
    assert invalid[0] == 200 and "All teams must have 2 debaters" in invalid[2], invalid[:2]
    updated = submit(rust_root, f"{team}/edit", fields + [["debaters", id] for id in member_ids])
    assert updated[:2] == [200, team], updated[:2]
    driver.get(f"{django_root}{team}/edit")
    assert driver.find_element("id", "id_short_name").get_attribute("value") == fields[0][1]
    driver.delete_all_cookies()


def assert_school_admin(driver, django, rust, fixture):
    rust_root = f"http://{HOST}:{rust.port}"
    django_root = f"http://{HOST}:{django.port}"
    manage = "/core/manage-school-admins/"
    dashboard = "/core/school-admin/"
    school = fixture["role_school"]

    def login(token):
        driver.get(f"{rust_root}/games/")
        driver.add_cookie({"name": "sessionid", "value": token})

    def post(page, endpoint, values):
        driver.get(f"{rust_root}{page}")
        return driver.execute_async_script("""
            const [action, values, done] = arguments;
            const token = document.querySelector("[name=csrfmiddlewaretoken]")?.value || window.csrfToken;
            if (!token) return done({status:0, text:document.body.textContent.trim().slice(0, 200)});
            const data = new URLSearchParams({...values, csrfmiddlewaretoken:token});
            fetch(action, {method:"POST",body:data}).then(async response => {
                const contentType = response.headers.get("Content-Type") || "";
                const result = contentType.includes("application/json") ? await response.json() : await response.text();
                done({status:response.status, redirected:response.redirected, result});
            }, () => done({status:0}));
        """, endpoint, values)

    login(django_session())
    values = {"school_id": school, "user_id": fixture["users"]["ordinary"]}
    added = post(manage, f"{manage}add/", values)
    assert added["status"] == 200 and added["result"]["created"] and added["result"]["admin"]["primary"], added
    first = added["result"]["admin"]["id"]
    again = post(manage, f"{manage}add/", values)
    assert again["result"]["admin"]["id"] == first and not again["result"]["created"], again
    login(fixture["sessions"]["ordinary"])
    values["user_id"] = fixture["users"]["account"]
    second = post(dashboard, f"{manage}add/", values)
    assert second["status"] == 200 and not second["result"]["admin"]["primary"], second
    account = second["result"]["admin"]["id"]
    login(fixture["sessions"]["account"])
    denied = post(dashboard, f"{manage}remove/", {"school_admin_id": first})
    assert denied["status"] == 403 and denied["result"]["error"] == "You can only remove yourself.", denied
    login(fixture["sessions"]["ordinary"])
    transferred = post(dashboard, f"{manage}primary/", {"school_admin_id": account})
    assert transferred["status"] == 200 and transferred["result"]["demoted_admin_id"] == first, transferred
    login(fixture["sessions"]["account"])
    path = f"{dashboard}{school}/debater/create/"
    for index in range(6):
        form = {"first_name":"Role", "last_name":f"Fixture{index}", "first_season":"2025",
                "latest_season":"2026", "status":"1"}
        result = post(path, path, form)
        if index == 5:
            assert result["status"] == 403 and "daily limit of 5 new debaters" in result["result"], result
        else:
            assert result["status"] == 200 and result["redirected"], (index, result["status"])
    driver.get(f"{django_root}{dashboard}")
    assert "Role Fixture4" in driver.find_element("tag name", "body").text
    login(fixture["sessions"]["ordinary"])
    removed = post(dashboard, f"{manage}remove/", {"school_admin_id": first})
    assert removed["status"] == 200 and removed["result"]["success"], removed
    driver.delete_all_cookies()


def assert_speaks_statistics(driver, django, rust, fixture):
    path = "/core/admin/speaks-stats/"
    driver.delete_all_cookies()
    driver.get(f"http://{HOST}:{rust.port}{path}")
    assert urlsplit(driver.current_url).path == "/auth/login/"
    driver.add_cookie({"name": "sessionid", "value": fixture["sessions"]["ordinary"]})
    driver.get(f"http://{HOST}:{rust.port}{path}")
    assert driver.find_element("tag name", "h1").text == "Permission denied"
    driver.add_cookie({"name": "sessionid", "value": django_session()})

    def snapshot(server):
        driver.get(f"http://{HOST}:{server.port}{path}")
        assert driver.find_element("tag name", "h1").text == "Speaks Statistics"
        lookup = driver.find_element("css selector", ".speaks-score-lookup input")
        driver.execute_script("""
            arguments[0].value = '29.4';
            arguments[0].dispatchEvent(new Event('input', {bubbles:true}));
        """, lookup)
        return driver.execute_script("""
            return {
                seasons: [...document.querySelectorAll('section[aria-labelledby^=season-]')].map(section => ({
                    title: section.querySelector('h2').textContent,
                    summary: section.querySelector('p').textContent.trim(),
                    tables: [...section.querySelectorAll('table')].map(table =>
                        [...table.rows].map(row => [...row.cells].map(cell => cell.textContent.replace(/\\s+/g, ' ').trim()))),
                    chartPaths: [...section.querySelectorAll('svg path')].map(path => path.getAttribute('d')),
                    thresholds: [...section.querySelectorAll('script[type="application/json"]')].map(script =>
                        JSON.parse(script.textContent).map(row => ({...row, score:Number(row.score),
                            percentile:Math.round(Number(row.percentile)*1e10)/1e10}))),
                })),
                lookup: document.querySelector('.speaks-score-lookup output').textContent,
            };
        """)

    expected, observed = snapshot(django), snapshot(rust)
    assert len(expected["seasons"]) >= 1
    assert expected["lookup"] == observed["lookup"], (expected["lookup"], observed["lookup"])
    for index, (left, right) in enumerate(zip_longest(expected["seasons"], observed["seasons"])):
        assert left is not None and right is not None, index
        for key in left:
            if isinstance(left[key], list):
                for subindex, (reference, candidate) in enumerate(zip_longest(left[key], right[key])):
                    assert reference == candidate, (left["title"], key, subindex, str(reference)[:240], str(candidate)[:240])
            else:
                assert left[key] == right[key], (index, key, left[key], right[key])
    driver.delete_all_cookies()


def seed_elo():
    code = """
from datetime import date
from django import setup
setup()
from core.models import Debater, QUAL, Round, RoundStats, School, Team, Tournament
school = School.objects.create(name="Disposable ELO school", short_name="ELO School")
active = [Debater.objects.create(first_name=f"Elo Fixture {index:02}", last_name="Speaker",
    school=school, first_season="2024", latest_season="2026", elo_manual_opt="opt_in")
    for index in range(56)]
partnerships = []
for index in range(0, len(active), 2):
    team = Team.objects.create(name=f"Elo Fixture {index:02}")
    team.debaters.add(active[index], active[index + 1])
    partnerships.append(team)
unqualified = active[0]
unqualified.elo_manual_opt = "opt_out"
unqualified.save(update_fields=["elo_manual_opt"])
pro_am = active[2]
pro_am.first_season = "2026"
pro_am.save(update_fields=["first_season"])
for year in (2024, 2026):
    event = Tournament.objects.create(host=school, date=date(year, 9, 10),
        season=str(year), manual_name=f"ELO fixture {year}")
    for index in range(0, len(partnerships), 2):
        gov, opp = partnerships[index:index + 2]
        round_number = index // 2 + 1
        is_elim = year == 2026 and index % 4 == 0
        stage = Round.Stage.OUTROUND if is_elim else Round.Stage.PRELIM
        stage_tag = "E" if is_elim else "P"
        if round_number == 2:
            for _ in range(2):
                Round.objects.create(tournament=event, gov=gov, opp=opp, victor=Round.GOV,
                    round_number=round_number, stage=stage, round_label=f"{stage_tag}{round_number}",
                    import_origin="forum")
        else:
            debate = Round.objects.create(tournament=event, gov=gov, opp=opp,
                victor=Round.GOV if year == 2024 else Round.OPP, round_number=round_number,
                stage=stage, round_label=f"{stage_tag}{round_number}")
            if round_number == 3 and not is_elim:
                RoundStats.objects.create(round=debate, debater=active[8], speaks=27.5, ranks=1)
        Round.objects.create(tournament=event, gov=gov, opp=opp, victor=Round.GOV_VIA_FORFEIT,
            round_number=round_number + 15)
unrated = Tournament.objects.create(host=school, date=date(2026, 9, 11),
    season="2026", manual_name="Disposable unresolved ELO round")
Round.objects.create(tournament=unrated, gov=partnerships[0], opp=partnerships[1], victor=Round.UNKNOWN)
QUAL.objects.get_or_create(debater=active[-1], season="2026")
"""
    subprocess.run([str(ROOT / "venv/bin/python"), "-c", code],
                   env={**os.environ, "SECRET_KEY": "disposable-local-parity-key"},
                   check=True, capture_output=True)


class LocalSMTP(StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"220 local parity SMTP\r\n")
        while data := self.rfile.readline():
            command = data.split(maxsplit=1)[0].upper()
            if command in (b"EHLO", b"HELO", b"MAIL", b"RCPT", b"RSET", b"NOOP"):
                self.wfile.write(b"250 okay\r\n")
            elif command == b"DATA":
                self.wfile.write(b"354 send the message\r\n")
                lines = []
                while (line := self.rfile.readline()) and line != b".\r\n":
                    lines.append(line[1:] if line.startswith(b"..") else line)
                self.server.messages.put(BytesParser(policy=policy.default).parsebytes(b"".join(lines)))
                self.wfile.write(b"250 fixture queued\r\n")
            elif command == b"QUIT":
                self.wfile.write(b"221 goodbye\r\n")
                return
            else:
                self.wfile.write(b"502 unknown command\r\n")


@contextmanager
def smtp_sink():
    with ThreadingTCPServer((HOST, 0), LocalSMTP) as server:
        server.daemon_threads = True
        server.messages = Queue()
        thread = Thread(target=server.serve_forever, daemon=True)
        port = server.server_address[1]
        exports = {"BLACK_ROD_SMTP_URL":f"smtp://{HOST}:{port}", "BLACK_ROD_PARITY_SMTP_PORT": str(port),
                   "BLACK_ROD_FROM_EMAIL":"parity@example.test"}
        prior = {key:os.environ.get(key) for key in exports}
        os.environ.update(exports)
        thread.start()
        try:
            yield server.messages
        finally:
            for key, value in prior.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            server.shutdown()
            thread.join()


def assert_account_entry_and_email(driver, django, rust, smtp):
    from selenium.webdriver.support.expected_conditions import staleness_of
    import re

    signup = "/accounts/signup/"
    login = "/accounts/login/"
    logout = "/accounts/logout/"
    email = "/accounts/email/"
    username = "rust_account_workflow"
    starting = "y8#Fg2m!Dv79"
    roots = {server.port: f"http://{HOST}:{server.port}" for server in (django,rust)}

    def open_page(server, path):
        driver.get(roots[server.port] + path)

    def fill(name, value):
        field = driver.find_element("name", name)
        field.clear()
        field.send_keys(value)

    def submit(selector='form button[type="submit"]'):
        old = driver.find_element("tag name","html")
        driver.find_element("css selector",selector).click()
        WebDriverWait(driver,10).until(document_replaced(old))

    driver.delete_all_cookies()
    open_page(rust, signup + "?next=/games/")
    fill("username",username)
    fill("email","workflow-initial@example.test")
    fill("password1",starting)
    fill("password2",starting)
    submit()
    assert urlsplit(driver.current_url).path == "/games/"
    open_page(django,email)
    assert "workflow-initial@example.test" in driver.find_element("tag name","body").text

    for sender,receiver,address in (
        (rust,django,"rust-confirmed@example.test"),
        (django,rust,"django-confirmed@example.test"),
    ):
        open_page(sender,email)
        driver.find_element("id","id_email").send_keys(address)
        submit('button[name="action_add"]')
        message = smtp.get(timeout=10)
        assert message["To"].addresses[0].addr_spec == address
        assert "Please Confirm Your Email Address" in message["Subject"]
        contents = message.get_body(preferencelist=("plain",)).get_content()
        link = re.search(r"/accounts/confirm-email/[-:\w]+/", contents)
        assert link, contents
        open_page(receiver,link.group())
        assert "Confirm Email Address" in driver.find_element("tag name","h1").text
        assert address in driver.find_element("tag name","body").text
        submit()
        open_page(django,email)
        radio = driver.find_element("css selector",f'input[value="{address}"]')
        label = driver.find_element("css selector",f'label[for="{radio.get_attribute("id")}"]')
        assert "Verified" in label.text and "Unverified" not in label.text

    open_page(rust,email)
    driver.find_element("css selector",'input[value="rust-confirmed@example.test"]').click()
    submit('button[name="action_primary"]')
    assert "Primary email address set." in driver.find_element("tag name","body").text
    open_page(rust,logout + "?next=/games/")
    submit()
    assert urlsplit(driver.current_url).path == "/games/"
    assert not driver.get_cookie("sessionid")
    open_page(rust,login + "?next=/games/")
    fill("login",username.upper())
    fill("password",starting)
    submit()
    assert urlsplit(driver.current_url).path == "/games/"
    open_page(django,email)
    assert "rust-confirmed@example.test" in driver.find_element("tag name","body").text
    db = Path(os.environ["BR_STATE"]) / "dev.sqlite3"
    with sqlite3.connect(f"{db.as_uri()}?mode=ro",uri=True) as connection:
        user = connection.execute("SELECT id,email FROM core_user WHERE username=?",(username,)).fetchone()
        assert user[1] == "rust-confirmed@example.test"
        addresses = connection.execute('SELECT email,verified,"primary" FROM account_emailaddress WHERE user_id=? ORDER BY email',(user[0],)).fetchall()
        assert addresses == [("django-confirmed@example.test",1,0),("rust-confirmed@example.test",1,1),("workflow-initial@example.test",0,0)]
    driver.delete_all_cookies()


def assert_password_accounts(driver, django, rust, smtp):
    script = """
from django import setup
setup()
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.auth.hashers import PBKDF2SHA1PasswordHasher
import json
data = {}
for label in ("rust-change", "django-change", "oauth-set", "rust-reset", "django-reset", "legacy-hash",
              "legacy-rust-reset", "legacy-django-reset", "legacy-rust-change", "legacy-django-change"):
    email = f"{label}@example.test"
    user = get_user_model().objects.create_user(username=label, email=email,
        password=None if label == "oauth-set" else "kN3%jku8^Qm2")
    if label == "legacy-hash":
        user.password = PBKDF2SHA1PasswordHasher().encode("kN3%jku8^Qm2", "fixture-legacy", iterations=3000)
        user.save(update_fields=["password"])
    EmailAddress.objects.create(user=user,email=email,verified=True,primary=True)
    s = SessionStore()
    s.update({"_auth_user_id":str(user.pk),"_auth_user_backend":"django.contrib.auth.backends.ModelBackend",
              "_auth_user_hash":user.get_session_auth_hash()})
    s.save()
    data[label] = s.session_key
print(json.dumps(data))
"""
    output = subprocess.check_output([str(ROOT / "venv/bin/python"), "-c", script],
        env={**os.environ, "SECRET_KEY": "disposable-local-parity-key"}, text=True)
    sessions = json.loads(output.splitlines()[-1])
    change = "/accounts/password/change/"
    reset = "/accounts/password/reset/"
    done = "/accounts/password/reset/key/done/"
    starting, new = "kN3%jku8^Qm2", "u5!MNaSqzV7d2"

    def page(server, path, login=None):
        driver.get(f"http://{HOST}:{server.port}{path}")
        if login:
            driver.add_cookie({"name":"sessionid","value":sessions[login]})
            driver.get(f"http://{HOST}:{server.port}{path}")

    def type_in(fields):
        for key,value in fields.items():
            field = driver.find_element("id",f"id_{key}")
            driver.execute_script("arguments[0].value = arguments[1];", field, value)
        form = driver.find_element("css selector", "main form")
        form.find_element("css selector", 'button[type="submit"]').click()
        WebDriverWait(driver, 10).until(document_replaced(form))

    def normalized():
        return driver.execute_script("""
            return {
                title:document.title.trim(),heading:document.querySelector('h1').innerText,
                links:[...document.querySelectorAll('body a')].map(a=>[a.innerText.trim(),a.pathname]),
                fields:[...document.querySelectorAll('form input')].filter(input=>input.type!=='hidden').map(input=>
                    [input.name,input.type,input.placeholder,input.autocomplete,input.required]),
                buttons:[...document.querySelectorAll('form button')].map(b=>b.innerText.trim()),
                errors:[...document.querySelectorAll('.errorlist li')].map(row=>row.innerText.trim())
            }
        """)

    snapshots = []
    for server,label in [(django,"django-change"),(rust,"rust-change")]:
        driver.delete_all_cookies()
        page(server,change,label)
        before = normalized()
        type_in({"oldpassword":"incorrect","password1":"1234","password2":"1234"})
        invalid = normalized()
        assert "Please type your current password." in invalid["errors"]
        assert "This password is entirely numeric." in invalid["errors"]
        snapshots.append((before,invalid))
        old_session = driver.get_cookie("sessionid")["value"]
        type_in({"oldpassword":starting,"password1":new,"password2":new})
        assert "Password successfully changed." in driver.page_source
        assert driver.get_cookie("sessionid")["value"] != old_session
        page(django if server is rust else rust,change)
        assert driver.find_element("tag name","h1").text == "Change Password"
    assert snapshots[0] == snapshots[1], snapshots

    driver.delete_all_cookies()
    page(rust,change,"oauth-set")
    assert urlsplit(driver.current_url).path == "/accounts/password/set/"
    type_in({"password1":new,"password2":new})
    assert "Password successfully set." in driver.page_source

    def reset_from(producer, consumer, label):
        driver.delete_all_cookies()
        page(producer, reset)
        type_in({"email":f"{label}@example.test"})
        assert urlsplit(driver.current_url).path == reset + "done/"
        message = smtp.get(timeout=5)
        assert message["To"].addresses[0].addr_spec == f"{label}@example.test"
        source = next(line.strip() for line in message.get_content().splitlines()
            if line.startswith("http") and "/accounts/password/reset/key/" in line)
        target = urlsplit(source).path
        page(consumer, target)
        assert urlsplit(driver.current_url).path.endswith("-set-password/")
        type_in({"password1":new,"password2":new})
        assert urlsplit(driver.current_url).path == done
        assert "Your password is now changed." in driver.page_source
        page(producer,target)
        assert driver.find_element("tag name","h1").text == "Bad Token"

    reset_from(django,rust,"django-reset")
    reset_from(rust,django,"rust-reset")

    def submit_legacy(fields):
        form = driver.find_element("css selector", "#content form")
        for key, value in fields.items():
            form.find_element("name", key).send_keys(value)
        form.find_element("css selector", 'input[type="submit"]').click()
        WebDriverWait(driver, 10).until(document_replaced(form))

    for source, target, label in ((django, rust, "django"), (rust, django, "rust")):
        driver.delete_all_cookies()
        address = f"legacy-{label}-reset@example.test"
        page(source, "/auth/password_reset/")
        submit_legacy({"email": address})
        assert urlsplit(driver.current_url).path == "/auth/password_reset/done/"
        message = smtp.get(timeout=5)
        assert message["To"].addresses[0].addr_spec == address
        link = next(line.strip() for line in message.get_content().splitlines()
                    if line.startswith("http") and "/auth/reset/" in line)
        route = urlsplit(link).path
        page(target, route)
        assert urlsplit(driver.current_url).path.endswith("/set-password/")
        submit_legacy({"new_password1": new, "new_password2": new})
        assert urlsplit(driver.current_url).path == "/auth/reset/done/"
        page(source, route)
        assert "The password reset link was invalid" in driver.page_source

        driver.delete_all_cookies()
        page(source, "/auth/password_change/", f"legacy-{label}-change")
        old_session = driver.get_cookie("sessionid")["value"]
        submit_legacy({"old_password": starting, "new_password1": new, "new_password2": new})
        assert urlsplit(driver.current_url).path == "/auth/password_change/done/"
        assert driver.get_cookie("sessionid")["value"] != old_session
        page(target, "/auth/password_change/done/")
        assert "Your password was changed." in driver.page_source

    driver.delete_all_cookies()
    page(rust,"/auth/login/?next=/games/")
    driver.find_element("id","id_username").send_keys("legacy-hash")
    driver.find_element("id","id_password").send_keys(starting)
    driver.find_element("id","id_password").submit()
    WebDriverWait(driver,10).until(lambda browser: urlsplit(browser.current_url).path == "/games/")
    page(django,change)
    assert driver.find_element("tag name","h1").text == "Change Password"
    db = Path(os.environ["BR_STATE"]) / "dev.sqlite3"
    with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT password FROM core_user WHERE username='legacy-hash'").fetchone()[0].startswith(
            "pbkdf2_sha256$1000000$")
    driver.delete_all_cookies()


def assert_elo_dashboard(driver, django, rust, fixture):
    path = "/core/elo/"
    login_cookie = django_session()
    driver.delete_all_cookies()
    for server in (django, rust):
        driver.get(f"http://{HOST}:{server.port}{path}")
        assert urlsplit(driver.current_url).path == "/auth/login/"
        driver.add_cookie({"name": "sessionid", "value": fixture["sessions"]["ordinary"]})
        driver.get(f"http://{HOST}:{server.port}{path}")
        assert driver.find_element("tag name", "h1").text.strip() == "Permission denied"
        driver.delete_all_cookies()

    def snapshot():
        return driver.execute_script("""
            return {
                badges: [...document.querySelectorAll('.elo-dashboard .badge-info')].map(node => node.innerText),
                rows: [...document.querySelectorAll('#elo-rankings-table tbody tr')].map(row =>
                    [...row.cells].map(td => [td.innerText.trim(), [...td.querySelectorAll('a')].map(a => a.pathname)])),
                currentPage: document.querySelector('.elo-dashboard h5 + .text-muted')?.innerText,
                pagination: [...document.querySelectorAll('nav[aria-label="ELO rankings pagination"] a')].map(a => a.search),
                controls: [...document.querySelectorAll('.elo-dashboard input:not([type=hidden])')].map(input =>
                    [input.id,input.type,input.value,input.checked]),
                shares: ['win', 'loss'].map(key => document.getElementById('higher-elo-' + key + '-share-output').innerText),
                seasons: ['compute', 'active'].map(key => document.getElementById(key + '-season-range-output').innerText),
                notices: [...document.querySelectorAll('.elo-dashboard .alert-danger')].map(node => node.innerText.trim()),
            };
        """)

    def submit(settings=None, check=()):
        for key, value in (settings or {}).items():
            control = driver.find_element("id", f"id_elo-{key}")
            driver.execute_script("""
                arguments[0].value = String(arguments[1]);
                arguments[0].dispatchEvent(new Event('input', {bubbles:true}));
            """, control, value)
        for name in check:
            driver.find_element("id", f"id_elo-{name}").click()
        form = driver.find_element("css selector", ".elo-dashboard form")
        form.find_element("css selector", 'button[type="submit"]').click()
        WebDriverWait(driver, 10).until(document_replaced(form))
        return snapshot()

    def visit(server):
        driver.delete_all_cookies()
        driver.get(f"http://{HOST}:{server.port}{path}")
        driver.add_cookie({"name": "sessionid", "value": login_cookie})
        driver.get(f"http://{HOST}:{server.port}{path}")
        initial = snapshot()
        default_run = submit()
        assert len(default_run["rows"]) == 50, (default_run["badges"], default_run["notices"], len(default_run["rows"]), server.port)
        assert len(default_run["badges"]) == 4
        assert default_run["pagination"] == ["?page=2"]
        driver.find_element("css selector", '[data-sort-key="rank"]').click()
        descending = snapshot()["rows"]
        assert [int(row[0][0]) for row in descending] == list(range(50, 0, -1))
        driver.find_element("css selector", 'nav[aria-label="ELO rankings pagination"] a').click()
        second = snapshot()
        assert second["rows"][0][0][0] == "51"
        assert second["pagination"][0] == "?page=1"
        driver.get(f"http://{HOST}:{server.port}{path}")
        custom = submit({"season_start": 2024, "active_season_start": 2024, "min_outrounds": 1,
                         "higher_elo_win_share": 75, "higher_elo_loss_share": 25},
                        check=("exclude_proam_partnerships", "exclude_dino_rounds"))
        assert custom["shares"] == ["75%", "25%"]
        assert all(int(row[5][0]) >= 1 for row in custom["rows"]), (custom["controls"], custom["rows"][:3], custom["notices"])
        return initial, default_run, second, custom

    def open_as_admin(server, path):
        driver.get(f"http://{HOST}:{server.port}{path}")
        driver.add_cookie({"name": "sessionid", "value": login_cookie})
        driver.get(f"http://{HOST}:{server.port}{path}")

    def invalidate(server):
        open_as_admin(server, "/core/admin-tools/")
        form = driver.find_element("css selector", 'form[action="/core/admin/elo-cache/invalidate/"]')
        form.find_element("tag name", "button").click()
        WebDriverWait(driver, 10).until(document_replaced(form))
        assert urlsplit(driver.current_url).path == "/core/admin-tools/"

    shared = bool(os.environ.get("BLACK_ROD_PARITY_MEMCACHED_ADDR"))
    left = visit(django)
    if shared:
        open_as_admin(rust, path)
        across = snapshot()
        assert across["badges"] == left[-1]["badges"]
        assert across["rows"] == left[-1]["rows"]
        invalidate(rust)
        open_as_admin(django, path)
        assert not snapshot()["badges"]
    right = visit(rust)
    if shared:
        open_as_admin(django, path)
        across = snapshot()
        assert across["badges"] == right[-1]["badges"]
        assert across["rows"] == right[-1]["rows"]
        invalidate(django)
        open_as_admin(rust, path)
        assert not snapshot()["badges"]
    for step, (expected, observed) in enumerate(zip(left, right, strict=True)):
        for key in expected:
            if key == "rows":
                for index, (a, b) in enumerate(zip_longest(expected[key], observed[key])):
                    assert a == b, (step, key, index, a, b)
            else:
                assert expected[key] == observed[key], (step, key, expected[key], observed[key])
    driver.delete_all_cookies()


def assert_native_site_admin(driver, django, rust):
    login_cookie = django_session()
    root = f"http://{HOST}:{rust.port}"
    reference = f"http://{HOST}:{django.port}"
    school_admin = "/admin/core/school/"
    credentials = "/admin/core/agentcredential/"

    def submit(css):
        element = driver.find_element("css selector", css)
        old = driver.find_element("tag name", "html")
        element.click()
        WebDriverWait(driver, 10).until(document_replaced(old))

    driver.delete_all_cookies()
    driver.get(root + school_admin)
    assert urlsplit(driver.current_url).path == "/admin/login/"
    driver.add_cookie({"name":"sessionid","value":login_cookie})
    driver.get(root + school_admin + "add/")
    driver.find_element("id","id_name").send_keys("Disposable native admin school")
    driver.find_element("id","id_short_name").send_keys("Native admin")
    submit('input[name="_continue"]')
    edit = urlsplit(driver.current_url).path
    assert edit.endswith("/change/") and edit.startswith(school_admin)
    driver.get(reference + edit)
    assert driver.find_element("id","id_name").get_attribute("value") == "Disposable native admin school"
    driver.get(root + edit)
    field = driver.find_element("id","id_short_name")
    field.clear()
    field.send_keys("Admin renamed")
    submit('input[name="_continue"]')
    assert urlsplit(driver.current_url).path == edit
    history = edit.replace("/change/","/history/")
    actions = []
    for origin in (root, reference):
        driver.get(origin + history)
        actions.append(driver.find_elements("css selector","#change-history tbody tr td:last-child"))
        actions[-1] = [row.text for row in actions[-1]]
    assert actions[0] == actions[1] == ["Added.", "Changed Short name."], actions
    driver.get(root + edit)
    submit("a.deletelink")
    assert driver.title.startswith("Delete |") and "Are you sure" in driver.find_element("tag name","body").text
    submit('input[value="Yes, I’m sure"]')
    assert urlsplit(driver.current_url).path == school_admin
    assert "was deleted successfully" in driver.page_source

    driver.get(root + credentials + "add/")
    driver.find_element("id","id_name").send_keys("Disposable native admin credential")
    # Fixed future expiration keeps fixture behavior independent of the user's clock.
    driver.find_element("id","id_expires_at_0").send_keys("2035-01-01")
    driver.find_element("id","id_expires_at_1").send_keys("12:00:00")
    submit('input[name="_save"]')
    assert "Agent credential issued" in driver.title
    token = driver.find_element("css selector","#content code").text
    assert token.startswith("br_agent_")
    endpoint = "/api/agent/v1/"
    for server in (django,rust):
        status, _, _ = request(server, Case(
            endpoint,authorization=f"Bearer {token}",
        ))
        assert status == 200
    driver.get(root + credentials)
    checkbox = driver.find_element("xpath",
        "//tr[th/a[normalize-space()='Disposable native admin credential']]/td/input[@name='_selected_action']")
    checkbox.click()
    driver.find_element("css selector",'select[name="action"] option[value="revoke_credentials"]').click()
    submit('.actions button[type="submit"]')
    for server in (django,rust):
        status, _, _ = request(server, Case(
            endpoint,authorization=f"Bearer {token}",
        ))
        assert status in (401,403)
    driver.delete_all_cookies()


def assert_admin_model_saves(driver, django, rust):
    code = """
from datetime import date
from django import setup
setup()
from core.models import Debater, School, Tournament
import json

school = School.objects.create(name="Parity Model School", short_name="Parity")
pairs = []
for _ in range(2):
    tournament = Tournament.objects.create(host=school, date=date(2025, 10, 15), season="2025")
    debater = Debater.objects.create(first_name="Parity", last_name="Speaker", school=school)
    pairs.append((tournament.pk, debater.pk))
print(json.dumps(pairs))
"""
    result = subprocess.check_output([str(ROOT / "venv/bin/python"), "-c", code], text=True)
    pairs = json.loads(result.splitlines()[-1])
    driver.get(f"http://{HOST}:{django.port}/robots.txt")
    driver.add_cookie({"name": "sessionid", "value": django_session()})

    def save_edit(root, route):
        previous = driver.find_element("tag name", "html")
        driver.find_element("name", "_continue").click()
        WebDriverWait(driver, 10).until(document_replaced(previous))
        driver.get(root + route)

    for server, (tournament, debater) in zip((django, rust), pairs):
        root = f"http://{HOST}:{server.port}"
        route = f"/admin/core/tournament/{tournament}/change/"
        driver.get(root + route)
        driver.find_element("css selector", '#id_qual_type option[value="2"]').click()
        save_edit(root, route)
        assert driver.find_element("id", "id_name").get_attribute("value") == "Parity Model School IV"
        assert driver.find_element("id", "id_autoqual_bar").get_attribute("value") == "8"
        assert all(not driver.find_element("id", f"id_{field}").is_selected()
                   for field in ("toty", "soty", "noty", "qual"))

        route = f"/admin/core/debater/{debater}/change/"
        driver.get(root + route)
        first = driver.find_element("id", "id_first_name")
        first.clear()
        first.send_keys("<b>Updated</b>")
        driver.find_element("id", "id_dino_judge_contact_opt_in").click()
        driver.find_element("id", "id_dino_to_contact_opt_in").click()
        region = driver.find_element("id", "id_region")
        region.clear()
        region.send_keys('"west"')
        save_edit(root, route)
        assert driver.find_element("id", "id_first_name").get_attribute("value") == "Updated"
        assert not driver.find_element("id", "id_dino_judge_contact_opt_in").is_selected()
        assert driver.find_element("id", "id_dino_to_contact_opt_in").is_selected()
        assert json.loads(driver.find_element("id", "id_region").get_attribute("value")) == ["west"]
    driver.delete_all_cookies()


def assert_admin_preserves_linked_state(driver, django, rust):
    code = """
from django import setup
setup()
from core.models import Debater, DebaterAliasGroup, ImmaculateGridHiddenDebater, Team
import json

group = DebaterAliasGroup.objects.create(label="Admin linked privacy fixture")
first = Debater.objects.create(first_name="Privacy", last_name="Existing", alias_group=group)
second = Debater.objects.create(first_name="Roster", last_name="Second")
ImmaculateGridHiddenDebater.objects.create(debater=first)
ImmaculateGridHiddenDebater.objects.create(debater=second)
team = Team.objects.create(name="Reverse roster")
team.debaters.add(second)
team.debaters.add(first)
print(json.dumps((group.pk, first.pk, second.pk, team.pk)))
"""
    output = subprocess.check_output([str(ROOT / "venv/bin/python"), "-c", code], text=True)
    group, first, second, team = json.loads(output.splitlines()[-1])
    db = Path(os.environ["BR_STATE"]) / "dev.sqlite3"
    root = f"http://{HOST}:{rust.port}"
    driver.get(root + "/robots.txt")
    driver.add_cookie({"name": "sessionid", "value": django_session()})

    def save():
        previous = driver.find_element("tag name", "html")
        driver.find_element("name", "_continue").click()
        WebDriverWait(driver, 10).until(document_replaced(previous))

    with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as connection:
        def hidden(debater):
            return connection.execute(
                "SELECT 1 FROM core_immaculategridhiddendebater WHERE debater_id=?", (debater,)).fetchone()

        driver.get(root + "/admin/core/debater/add/")
        driver.find_element("id", "id_first_name").send_keys("Privacy new member")
        driver.find_element("css selector", f'#id_alias_group option[value="{group}"]').click()
        assert not driver.find_element("id", "id_immaculate_hidden").is_selected()
        save()
        edit = urlsplit(driver.current_url).path
        assert edit.endswith("/change/"), edit
        assert hidden(first) and hidden(second)
        for server in (django, rust):
            driver.get(f"http://{HOST}:{server.port}{edit}")
            assert driver.find_element("id", "id_immaculate_hidden").is_selected()
        driver.find_element("id", "id_immaculate_hidden").click()
        save()
        assert not hidden(first) and hidden(second)

        def roster():
            return connection.execute("SELECT id,debater_id FROM core_team_debaters WHERE team_id=? ORDER BY id",
                                      (team,)).fetchall()

        before = roster()
        assert [member for _, member in before] == [second, first]
        driver.get(root + f"/admin/core/team/{team}/change/")
        field = driver.find_element("id", "id_short_name")
        field.clear()
        field.send_keys("Roster edit")
        save()
        assert roster() == before
        assert driver.find_element("id", "id_short_name").get_attribute("value") == "Roster edit"
    driver.delete_all_cookies()


def assert_admin_across_pages(driver, rust):
    root = f"http://{HOST}:{rust.port}"
    code = """
from datetime import timedelta
from django import setup
setup()
from django.contrib.auth import get_user_model
from django.utils import timezone
from core.models import AgentCredential, School

user = get_user_model().objects.get(username="tab")
for index in range(102):
    AgentCredential.issue(admin=user, name=f"Pagination parity {index}",
        expires_at=timezone.now() + timedelta(days=1), can_write=index != 101)
    label = f"Pagination parity school {index}" if index != 101 else "Unselected control school"
    School.objects.create(name=label, short_name=f"Bulk page {index}")
"""
    subprocess.run([str(ROOT / "venv/bin/python"), "-c", code], check=True)
    driver.get(root + "/robots.txt")
    driver.add_cookie({"name": "sessionid", "value": django_session()})

    def select_all(route, action):
        driver.get(root + route)
        driver.find_element("id", "action-toggle").click()
        driver.find_element("css selector", ".actions .question a").click()
        driver.find_element("css selector", f'select[name="action"] option[value="{action}"]').click()
        previous = driver.find_element("tag name", "html")
        driver.find_element("css selector", ".actions button").click()
        WebDriverWait(driver, 10).until(document_replaced(previous))

    credentials = "/admin/core/agentcredential/?can_write__exact=1"
    select_all(credentials, "revoke_credentials")
    schools = "/admin/core/school/?q=Pagination+parity+school"
    select_all(schools, "delete_selected")
    assert len(driver.find_elements("css selector", 'input[name="_selected_action"]')) == 101
    previous = driver.find_element("tag name", "html")
    driver.find_element("css selector", 'input[name="post"] ~ input[type="submit"]').click()
    WebDriverWait(driver, 10).until(document_replaced(previous))

    subprocess.run([str(ROOT / "venv/bin/python"), "-c", """
from django import setup
setup()
from core.models import AgentCredential, School

credentials = AgentCredential.objects.filter(name__startswith="Pagination parity ")
assert credentials.filter(can_write=True, revoked_at__isnull=False).count() == 101
assert credentials.filter(can_write=False, revoked_at__isnull=True).count() == 1
assert not School.objects.filter(name__startswith="Pagination parity school ").exists()
assert School.objects.filter(name="Unselected control school").exists()
"""], check=True)
    driver.delete_all_cookies()


def assert_admin_attachments(driver, django, rust):
    root = Path(os.environ["BR_STATE"])
    file = root / "fixture-attachment.txt"
    file.write_text("Fabricated attachment for the parity test.\n")
    admin = django_session()
    route = "/admin/django_summernote/attachment/"
    for source, target in ((django, rust), (rust, django)):
        driver.get(f"http://{HOST}:{source.port}/robots.txt")
        driver.delete_all_cookies()
        driver.add_cookie({"name": "sessionid", "value": admin})
        driver.get(f"http://{HOST}:{source.port}{route}add/")
        upload = driver.find_element("name", "file")
        assert upload.get_attribute("type") == "file"
        upload.send_keys(str(file))
        driver.find_element("name", "_save").click()
        WebDriverWait(driver, 10).until(lambda browser: urlsplit(browser.current_url).path == route)
        with sqlite3.connect(root / "dev.sqlite3") as connection:
            record, name, stored = connection.execute(
                "SELECT id,name,file FROM django_summernote_attachment ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert name == file.name and (root / "media" / stored).read_bytes() == file.read_bytes()
        public = f"http://{HOST}:{rust.port}/{stored}"
        with urlopen(public) as response:
            assert response.read() == file.read_bytes()
        driver.get(f"http://{HOST}:{target.port}{route}{record}/change/")
        assert driver.find_element("css selector", ".field-file a").get_attribute("href").endswith(stored)
        label = driver.find_element("name", "name")
        label.clear()
        label.send_keys("Renamed local attachment")
        driver.find_element("name", "_save").click()
        WebDriverWait(driver, 10).until(lambda browser: urlsplit(browser.current_url).path == route)
        with sqlite3.connect(root / "dev.sqlite3") as connection:
            assert connection.execute(
                "SELECT name,file FROM django_summernote_attachment WHERE id=?", (record,)
            ).fetchone() == ("Renamed local attachment", stored)
    driver.delete_all_cookies()


def assert_admin_speaker_visibility(driver, django, rust):
    route = "/admin/core/speakerresult/?q=Third+Result"
    origins = [f"http://{HOST}:{server.port}" for server in (django, rust)]
    driver.get(origins[1] + "/robots.txt")
    driver.delete_all_cookies()
    driver.add_cookie({"name":"sessionid","value":django_session()})
    for source, candidate in ((1, 0), (0, 1)):
        driver.get(origins[source] + route)
        fields = driver.find_elements("css selector", "#result_list input[name$='-hidden_from_profile']")
        assert len(fields) == 1
        expected = not fields[0].is_selected()
        record = driver.find_element("id", "id_form-0-id").get_attribute("value")
        fields[0].click()
        previous = driver.find_element("tag name", "html")
        driver.find_element("name", "_save").click()
        WebDriverWait(driver, 10).until(document_replaced(previous))
        assert "1 speaker result was changed successfully." in driver.find_element("tag name", "body").text
        driver.get(origins[candidate] + route)
        assert driver.find_element("id", "id_form-0-hidden_from_profile").is_selected() == expected
        driver.get(origins[candidate] + f"/admin/core/speakerresult/{record}/history/")
        assert "Changed Hidden from profile." in driver.find_element("tag name", "body").text
    driver.delete_all_cookies()


def assert_admin_topic_merges(driver, django, rust):
    code = """
from django import setup
setup()
from core.models import Motion, Video
import json

motion = Motion.objects.filter(text__startswith="This House would use a disposable").get()
video = Video.objects.filter(link__startswith="https://videos.example.test/access-").order_by("id").first()
for record in (motion, video):
    record.tags.add("Parity topic alpha", "Parity topic zeta")
print(json.dumps([[motion.pk, video.pk]]))
"""
    output = subprocess.check_output([str(ROOT / "venv/bin/python"), "-c", code], text=True)
    [(motion, video)] = json.loads(output.splitlines()[-1])
    driver.get(f"http://{HOST}:{rust.port}/robots.txt")
    driver.delete_all_cookies()
    driver.add_cookie({"name": "sessionid", "value": django_session()})
    for prefix, edit in (("/admin/core/motiontopic/", f"/admin/core/motion/{motion}/change/"),
                         ("/admin/taggit/tag/", f"/admin/core/video/{video}/change/")):
        for server in (django, rust):
            driver.get(f"http://{HOST}:{server.port}{edit}")
            tags = driver.find_element("id", "id_tags").get_attribute("value")
            assert '"Parity topic alpha"' in tags and '"Parity topic zeta"' in tags, tags
        driver.get(f"http://{HOST}:{rust.port}{prefix}?q=Parity+topic")
        checkboxes = driver.find_elements("css selector", ".action-select")
        assert len(checkboxes) == 2
        for checkbox in checkboxes:
            checkbox.click()
        driver.find_element("css selector", 'select[name="action"] option[value="merge_topics"]').click()
        old = driver.find_element("tag name", "html")
        driver.find_element("css selector", ".actions button").click()
        WebDriverWait(driver, 10).until(document_replaced(old))
        assert "Merged 1 topics into 'Parity topic alpha'." in driver.find_element("tag name", "body").text
        for server in (django, rust):
            driver.get(f"http://{HOST}:{server.port}{edit}")
            tags = driver.find_element("id", "id_tags").get_attribute("value")
            assert '"Parity topic alpha"' in tags and '"Parity topic zeta"' not in tags, tags
    driver.delete_all_cookies()


def assert_admin_formats(driver, django, rust):
    import base64
    code = """
from django import setup
setup()
from core.models import School
school = School.objects.create(name="Disposable Échange School",short_name="Échange, school")
print(school.id)
"""
    result = subprocess.check_output([str(ROOT / "venv/bin/python"), "-c", code],
        env={**os.environ,"SECRET_KEY":"disposable-local-parity-key"},text=True)
    existing = int(result.splitlines()[-1])
    credential = django_session()
    root = f"http://{HOST}:{rust.port}"
    query = "?" + urlencode({"q":"Échange"})
    route = "/admin/core/school/"
    formats = ("csv","xls","xlsx","tsv","ods","json","yaml","html")
    exports = {}

    def open_page(server, page):
        origin = f"http://{HOST}:{server.port}"
        driver.get(origin + "/robots.txt")
        driver.delete_all_cookies()
        driver.add_cookie({"name":"sessionid","value":credential})
        driver.get(origin + route + page)

    def download_format(index):
        return driver.execute_async_script("""
            const format = arguments[0], done = arguments[1];
            const csrfmiddlewaretoken = document.querySelector('#content input[name=csrfmiddlewaretoken]').value;
            fetch(location.href,{method:'POST',body:new URLSearchParams({csrfmiddlewaretoken,file_format:format})})
            .then(async response=>{
                const blob=await response.blob();
                const reader=new FileReader();
                reader.onload=()=>done({status:response.status,type:response.headers.get('Content-Type'),
                    attachment:response.headers.get('Content-Disposition'),blob:reader.result.split(',')[1]});
                reader.readAsDataURL(blob);
            }).catch(error=>done({status:0,error:String(error)}));
        """,str(index))

    for server in (django,rust):
        open_page(server,"export/" + query)
        downloads = []
        for index in range(len(formats)):
            download = download_format(index)
            assert download["status"] == 200 and download["attachment"].endswith(f'.{formats[index]}"'), download["attachment"]
            downloads.append(download)
        exports[server.port] = downloads
    django_exports = exports[django.port]
    rust_exports = exports[rust.port]
    for expected,actual in zip(django_exports,rust_exports):
        assert expected["type"] == actual["type"]
        assert expected["attachment"] == actual["attachment"]
    for index in (0,3,7):
        assert base64.b64decode(django_exports[index]["blob"]) == base64.b64decode(rust_exports[index]["blob"])
    for index in range(len(formats)):
        open_page(rust,"import/")
        incoming = django_exports[index]["blob"]
        draft = driver.execute_async_script("""
            const [encoded,format,ext,done] = arguments;
            const body = new FormData();
            body.set('csrfmiddlewaretoken',document.querySelector('#content input[name=csrfmiddlewaretoken]').value);
            body.set('input_format',format);
            body.set('import_file',new File([Uint8Array.from(atob(encoded), x=>x.charCodeAt(0))],'fixture.'+ext));
            fetch(location.href,{method:'POST',body}).then(async response=>{
                const html = new DOMParser().parseFromString(await response.text(),'text/html');
                done({ok:response.ok, preview:!!html.querySelector('input[name=confirm]'),
                      content:html.body.innerText,text:html.querySelector('.import-preview')?.textContent});
            }).catch(error=>done({error:String(error)}));
        """,incoming,str(index),formats[index])
        assert draft.get("ok") and draft.get("preview") and "Échange, school" in draft.get("text",""), (formats[index],draft)

    open_page(rust,"import/")
    from tempfile import NamedTemporaryFile
    with NamedTemporaryFile(mode="w",encoding="utf-8",suffix=".csv") as fixture:
        fixture.write("id,name,short_name,included_in_oty,temporary,synthetic\n")
        fixture.write(f"{existing},Disposable Échange School,Changed by import,1,0,0\n")
        fixture.write(",Disposable Imported School,New import,1,0,0\n")
        fixture.flush()
        driver.find_element("name","import_file").send_keys(fixture.name)
        driver.find_element("css selector",'select[name="input_format"] option[value="0"]').click()
        html = driver.find_element("tag name","html")
        driver.find_element("css selector",'#content input[value="Submit"]').click()
        WebDriverWait(driver,10).until(document_replaced(html))
    assert "Preview" in driver.find_element("id","content").text
    db = Path(os.environ["BR_STATE"]) / "dev.sqlite3"
    with sqlite3.connect(f"{db.as_uri()}?mode=ro",uri=True) as connection:
        assert connection.execute("SELECT short_name FROM core_school WHERE id=?",(existing,)).fetchone() == ("Échange, school",)
        assert connection.execute("SELECT id FROM core_school WHERE name='Disposable Imported School'").fetchone() is None
    html = driver.find_element("tag name","html")
    driver.find_element("name","confirm").click()
    WebDriverWait(driver,10).until(document_replaced(html))
    assert driver.current_url == root + route
    assert "Import finished, with 1 new and 1 updated schools." in driver.find_element("tag name","body").text
    open_page(django,f"{existing}/change/")
    assert driver.find_element("name","short_name").get_attribute("value") == "Changed by import"
    with sqlite3.connect(f"{db.as_uri()}?mode=ro",uri=True) as connection:
        assert connection.execute("SELECT short_name FROM core_school WHERE name='Disposable Imported School'").fetchone() == ("New import",)
        assert connection.execute("SELECT change_message FROM django_admin_log WHERE object_id=? ORDER BY id DESC LIMIT 1",
                                  (str(existing),)).fetchone() == ("update through import_export",)
    code = """
from datetime import date
from django import setup
setup()
from core.models import Debater, SOTY, School, SpeakerResult, Tournament
import json

school = School.objects.create(name="Parity speaker import school")
person = Debater.objects.create(first_name="Parity", last_name="SOTY", school=school)
event = Tournament.objects.create(host=school, season="2039", date=date(2039, 10, 1))
standing = SOTY.objects.create(season="2039", debater=person, place=1, points=8)
result = SpeakerResult.objects.create(tournament=event, debater=person, place=1)
print(json.dumps((standing.pk, result.pk, event.pk, person.pk)))
"""
    seeded = subprocess.check_output([str(ROOT / "venv/bin/python"), "-c", code], text=True)
    standing, award, tournament, debater = json.loads(seeded.splitlines()[-1])
    route = "/admin/core/soty/"
    rendered = []
    for server in (django, rust):
        open_page(server, "export/?season__exact=2039")
        export = download_format(0)
        assert export["status"] == 200, export
        rendered.append(base64.b64decode(export["blob"]))
    assert rendered[0] == rendered[1]
    assert str(standing).encode() in rendered[0]

    # The SOTY admin's configured import resource writes tournament speaker results.
    for server, place in ((django, 2), (rust, 3)):
        open_page(server, "import/")
        with NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".csv") as fixture:
            fixture.write("id,tournament,debater,type_of_place,place,tie,counts_for_points,hidden_from_profile\n")
            fixture.write(f"{award},{tournament},{debater},1,{place},0,1,0\n")
            fixture.flush()
            driver.find_element("name", "import_file").send_keys(fixture.name)
            driver.find_element("css selector", 'select[name="input_format"] option[value="0"]').click()
            previous = driver.find_element("tag name", "html")
            driver.find_element("css selector", '#content input[value="Submit"]').click()
            WebDriverWait(driver, 10).until(document_replaced(previous))
        assert "Preview" in driver.find_element("id", "content").text
        with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as connection:
            assert connection.execute("SELECT place FROM core_speakerresult WHERE id=?", (award,)).fetchone() == (place - 1,)
        previous = driver.find_element("tag name", "html")
        driver.find_element("css selector", "#content input[type='submit']").click()
        WebDriverWait(driver, 10).until(document_replaced(previous))
        with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as connection:
            assert connection.execute("SELECT place FROM core_speakerresult WHERE id=?", (award,)).fetchone() == (place,)
            assert connection.execute("SELECT place,points FROM core_soty WHERE id=?", (standing,)).fetchone() == (1, 8)
    driver.delete_all_cookies()


def assert_native_round_inlines(driver, django, rust):
    code = """
from django import setup
setup()
from core.models import Debater, ImportedRoundMetadata, Round, RoundStats
import json
original = Round.objects.order_by("id").first()
created = Round.objects.create(tournament=original.tournament, gov=original.gov, opp=original.opp,
    round_number=71, round_label="Disposable inline round")
people = list(Debater.objects.order_by("id")[:3])
discard = RoundStats.objects.create(round=created, debater=people[0], speaks="27.5", ranks="1", debater_role="PM")
keep = RoundStats.objects.create(round=created, debater=people[1], speaks="25.5", ranks="2", debater_role="MG")
metadata = ImportedRoundMetadata.objects.create(round=created, raw_result_code="Before")
print(json.dumps({"round":created.id,"discard":discard.id,"keep":keep.id,"new_debater":people[2].id,"metadata":metadata.id}))
"""
    output = subprocess.check_output([str(ROOT / "venv/bin/python"), "-c", code],
        env={**os.environ, "SECRET_KEY": "disposable-local-parity-key"}, text=True)
    records = json.loads(output.splitlines()[-1])
    route = f'/admin/core/round/{records["round"]}/change/'
    cookie = django_session()

    def open_page(server):
        root = f"http://{HOST}:{server.port}"
        driver.get(root + "/robots.txt")
        driver.delete_all_cookies()
        driver.add_cookie({"name": "sessionid", "value": cookie})
        driver.get(root + route)

    def submit():
        old = driver.find_element("tag name", "html")
        driver.find_element("name", "_continue").click()
        WebDriverWait(driver, 10).until(document_replaced(old))

    open_page(rust)
    driver.find_element("css selector", "#stats-group .add-row a").click()
    third = driver.find_element("name", "stats-2-debater")
    driver.execute_script("arguments[0].append(new Option('Fixture debater',arguments[1],true,true));",
                          third, str(records["new_debater"]))
    driver.find_element("name", "stats-2-speaks").send_keys("26.75")
    edit = driver.find_element("name","stats-1-speaks")
    edit.clear()
    edit.send_keys("28.125")
    driver.find_element("name","stats-0-DELETE").click()
    field = driver.find_element("name", "imported_metadata-0-raw_result_code")
    field.clear()
    field.send_keys("After")
    submit()
    assert "Please correct" not in driver.find_element("tag name","body").text
    assert "was changed successfully" in driver.find_element("tag name","body").text

    open_page(django)
    assert driver.find_element("name","imported_metadata-0-raw_result_code").get_attribute("value") == "After"
    assert driver.find_element("name","stats-0-speaks").get_attribute("value") == "28.1250"
    assert driver.find_element("name","stats-1-speaks").get_attribute("value") == "26.7500"

    open_page(rust)
    driver.find_element("css selector", '#id_stage option[value="outround"]').click()
    submit()
    db = Path(os.environ["BR_STATE"]) / "dev.sqlite3"
    with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT id,debater_id,stage,speaks,ranks,debater_role FROM core_roundstats WHERE round_id=? ORDER BY id",
            (records["round"],)).fetchall()
        assert len(rows) == 2 and rows[0][0] == records["keep"] and rows[1][1] == records["new_debater"], rows
        assert [row[2:] for row in rows] == [("outround", None, None, None)] * 2, rows
        assert connection.execute("SELECT raw_result_code FROM core_importedroundmetadata WHERE id=?",
                                  (records["metadata"],)).fetchone() == ("After",)
    driver.delete_all_cookies()


def assert_merge_suggestions(driver, django, rust):
    code = """
from django import setup
setup()
from core.models import Debater, School, SpeakerResult, Tournament
import json
school = School.objects.create(name="Disposable suggestion school")
tournament = Tournament.objects.first()
one = Debater.objects.create(first_name="UniqueFixture", last_name="Mergecandidate", school=school)
two = Debater.objects.create(first_name="UniqueFixture", last_name="Mergecandidate", school=school)
SpeakerResult.objects.create(debater=one, tournament=tournament, place=97)
print(json.dumps([one.id,two.id]))
"""
    output = subprocess.check_output([str(ROOT / "venv/bin/python"), "-c", code],
        env={**os.environ, "SECRET_KEY": "disposable-local-parity-key"}, text=True)
    first, second = json.loads(output.splitlines()[-1])
    route = "/core/admin/merge-suggestions/"
    fixture = f'tr[data-debater-one="{second}"][data-debater-two="{first}"]'
    cookie = django_session()
    rows = []
    for server in (django, rust):
        root = f"http://{HOST}:{server.port}"
        driver.get(root + "/robots.txt")
        driver.delete_all_cookies()
        driver.add_cookie({"name": "sessionid", "value": cookie})
        driver.get(root + route + "?refresh=1")
        row = driver.find_element("css selector", fixture)
        assert "100% Match" in row.text and "SAME SCHOOL" in row.text.upper()
        rows.append(driver.execute_script("""
            return [...document.querySelectorAll('#suggestions-tbody tr')]
              .map(row=>[row.dataset.debaterOne,row.dataset.debaterTwo,row.innerText.replace(/\\s+/g, " ").trim()]);
        """))
    assert rows[0] == rows[1], (len(rows[0]),len(rows[1]),next(
        ((a,b) for a,b in zip(*rows) if a!=b),None))
    driver.find_element("css selector", fixture + " .make-request-btn").click()
    WebDriverWait(driver, 10).until(lambda browser: not browser.find_elements("css selector", fixture))
    db = Path(os.environ["BR_STATE"]) / "dev.sqlite3"
    with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as connection:
        assert connection.execute(
            "SELECT primary_debater_id,secondary_debater_id FROM core_mergedebaterrequest WHERE primary_debater_id=?",
            (first,)).fetchone() == (first,second)
    driver.get(root + route)
    assert not driver.find_elements("css selector", fixture)
    driver.get(root + route)
    assert driver.find_element("css selector",'a[href="?refresh=1"]').is_displayed()
    driver.delete_all_cookies()


def assert_debater_aliases(driver, django, rust):
    code = """
from django import setup
setup()
from core.models import Debater, DebaterAliasGroup, School
import json
schools = [School.objects.create(name=f"Disposable alias school {i}") for i in range(3)]
first = DebaterAliasGroup.objects.create(label="First alias fixture")
second = DebaterAliasGroup.objects.create(label="Second alias fixture")
one = Debater.objects.create(first_name="Disposable", last_name="Aliascheck",
    school=schools[0], first_season="2025", latest_season="2026", alias_group=first)
two = Debater.objects.create(first_name="Disposable", last_name="Aliascheck",
    school=schools[1], first_season="2024", latest_season="2025", alias_group=second)
three = Debater.objects.create(first_name="Related", last_name="Aliascheck",
    school=schools[2], alias_group=first)
print(json.dumps({"one":one.id, "two":two.id, "three":three.id, "kept":second.id, "removed":first.id}))
"""
    output = subprocess.check_output([str(ROOT / "venv/bin/python"), "-c", code],
        env={**os.environ, "SECRET_KEY": "disposable-local-parity-key"}, text=True)
    records = json.loads(output.splitlines()[-1])
    route = "/core/admin/debater-aliases/"
    fixture = f'input[name="debater_one"][value="{records["two"]}"]'
    cookie = django_session()
    for server in (django, rust):
        driver.get(f"http://{HOST}:{server.port}/robots.txt")
        driver.delete_all_cookies()
        driver.add_cookie({"name": "sessionid", "value": cookie})
        driver.get(f"http://{HOST}:{server.port}{route}")
        form = driver.find_element("css selector", fixture).find_element("xpath", "..")
        assert form.find_element("name", "debater_two").get_attribute("value") == str(records["one"])
        assert "gap 0 yrs" in form.find_element("xpath", "../..").text.lower()
    form.find_element("tag name", "button").click()
    alert = WebDriverWait(driver, 10).until(
        lambda browser: browser.find_element("css selector",".alert-success"))
    assert "Linked Disposable Aliascheck" in alert.text
    assert not driver.find_elements("css selector", fixture)
    db = Path(os.environ["BR_STATE"]) / "dev.sqlite3"
    with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT alias_group_id FROM core_debater WHERE id IN (?,?,?)",
            (records["one"], records["two"], records["three"])).fetchall()
        assert rows == [(records["kept"],)] * 3
        assert connection.execute("SELECT id FROM core_debateraliasgroup WHERE id=?",
                                  (records["removed"],)).fetchone() is None
    driver.delete_all_cookies()


def assert_synthetic_suggestions(driver, django, rust):
    code = """
from django import setup
setup()
from core.models import Debater, School
import json
canonical = School.objects.create(name="Disposable Lakenford University", short_name="DLU")
source = School.all_objects.create(name="Disposable Lakenford College", short_name="DLU", synthetic=True, temporary=True)
first = Debater.objects.create(first_name="Zaybra",last_name="Quinder",school=canonical)
second = Debater.all_objects.create(first_name="Zaybra",last_name="Quindr",school=source,synthetic=True,temporary=True)
print(json.dumps({"canonical":canonical.id,"source":source.id,"target_debater":first.id,"source_debater":second.id}))
"""
    output = subprocess.check_output([str(ROOT / "venv/bin/python"), "-c", code],
        env={**os.environ, "SECRET_KEY": "disposable-local-parity-key"}, text=True)
    data = json.loads(output.splitlines()[-1])
    path = "/core/admin/synthetic-resolution-suggestions/"
    query = "?" + urlencode({
        "run": 1, "refresh": 1, "synthetic_debaters": data["source_debater"], "synthetic_schools": data["source"],
    })
    rust_base = f"http://{HOST}:{rust.port}"
    session = django_session()

    def open_page(server, route):
        root = f"http://{HOST}:{server.port}"
        driver.get(root + "/robots.txt")
        driver.delete_all_cookies()
        driver.add_cookie({"name":"sessionid","value":session})
        driver.get(root + route)

    def rows():
        return driver.execute_script("""
            return [...document.querySelectorAll('tr[data-synthetic-id]')].map(row=>[
                +row.dataset.syntheticId,+row.dataset.targetId,row.innerText.replace(/\\s+/g," ").trim()
            ]);
        """)

    open_page(rust, path)
    assert rows() == []
    for server in (django,rust):
        open_page(server, path + query)
        observed = rows()
        expected = {
            (data["source"],data["canonical"]),
            (data["source_debater"],data["target_debater"]),
        }
        assert expected.issubset({(row[0],row[1]) for row in observed}), observed
        if server == django:
            reference = observed
    assert reference == observed, (reference,observed)
    driver.get(rust_base + path + query.replace("&refresh=1",""))
    assert "Cached" in driver.find_element("tag name","body").text

    def submit(action, entity, source, target):
        return driver.execute_async_script("""
            const [path, action, entity_type, synthetic_id, target_id, done] = arguments;
            const csrfmiddlewaretoken = document.querySelector('[name=csrfmiddlewaretoken]').value;
            fetch(path, {method:'POST',body:new URLSearchParams({
                action,entity_type,synthetic_id,target_id,csrfmiddlewaretoken
            })}).then(response=>response.json()).then(done,error=>done({error:String(error)}));
        """, path, action, entity, source, target)

    for _ in range(2):
        rejected = submit("reject","debater",data["source_debater"],data["target_debater"])
        assert rejected["success"] and rejected["action"] == "reject", rejected
    driver.get(rust_base + path + query.replace("&refresh=1",""))
    assert (data["source_debater"],data["target_debater"]) not in {(row[0],row[1]) for row in rows()}
    resolved = submit("resolve","school",data["source"],data["canonical"])
    assert resolved["success"] and resolved["action"] == "resolve", resolved
    db = Path(os.environ["BR_STATE"]) / "dev.sqlite3"
    with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as connection:
        logs = connection.execute(
            "SELECT action,entity_type,reason,source_context,synthetic_snapshot FROM core_syntheticresolutionlog where synthetic_id IN (?,?) ORDER BY id",
            (data["source"],data["source_debater"])).fetchall()
        assert [(entry[0],entry[1]) for entry in logs] == [("rejected","debater"),("resolved","school")], logs
        assert json.loads(logs[0][3]) == {"source":"synthetic_resolution_suggestions"}
        assert json.loads(logs[0][4])["school_id"] == data["source"]
        assert connection.execute("SELECT school_id FROM core_debater WHERE id=?",
            (data["source_debater"],)).fetchone() == (data["canonical"],)
    driver.delete_all_cookies()


def assert_merge_requests(driver, django, rust, fixture):
    code = """
from django import setup
setup()
from core.models import Debater, School, SchoolAdmin, User
import json
school = School.objects.create(name="Disposable merge request school")
SchoolAdmin.objects.create(school=school,user_id=int(USER_ID))
one = Debater.objects.create(first_name="Merge",last_name="Alpha",school=school,latest_season="2026")
two = Debater.objects.create(first_name="Merge",last_name="Zeta",school=school,latest_season="2025")
print(json.dumps({"school":school.id, "one":one.id,"two":two.id}))
""".replace("USER_ID",repr(str(fixture["users"]["ordinary"])))
    output = subprocess.check_output([str(ROOT / "venv/bin/python"), "-c", code],
        env={**os.environ, "SECRET_KEY": "disposable-local-parity-key"}, text=True)
    data = json.loads(output.splitlines()[-1])
    create = "/core/merge-debaters/request/"
    review = "/core/merge-debaters/review/"
    regular = fixture["sessions"]["ordinary"]
    admin = django_session()

    def open_page(server, path, cookie):
        root = f"http://{HOST}:{server.port}"
        driver.get(root + "/robots.txt")
        driver.delete_all_cookies()
        driver.add_cookie({"name":"sessionid","value":cookie})
        driver.get(root+path)

    def submit(path, values):
        return driver.execute_async_script("""
            const [path, values, done] = arguments;
            values.csrfmiddlewaretoken = document.querySelector('input[name=csrfmiddlewaretoken]').value;
            fetch(path,{method:'POST',body:new URLSearchParams(values)}).then(async response=>{
                const body=new DOMParser().parseFromString(await response.text(),'text/html');
                done({status:response.status,path:new URL(response.url).pathname,redirected:response.redirected,
                    content:body.body.textContent.replace(/\s+/g,' ').trim()});
            },()=>done({status:0}));
        """, path, values)

    for server in (django,rust):
        open_page(server, create, regular)
        assert "Request Debater Merge" in driver.page_source
        options = driver.execute_script("""
            return [...document.querySelectorAll('#id_school_one option')].map(option=>option.text);
        """)
        assert options == ["---------","Disposable merge request school"]
        paths = "/core/debaters/autocomplete/merge?" + urlencode(
            {"q":"Merge","forward":json.dumps({"school":data["school"]})})
        found = driver.execute_async_script("""
            fetch(arguments[0]).then(response=>response.json()).then(arguments[1]);
        """, paths)
        assert [row["id"] for row in found["results"]] == [str(data["one"]),str(data["two"])]
        assert found["results"][0]["selected_text"] == f"<{data['one']}> Merge Alpha (Disposable merge request school)"
    open_page(rust,create,regular)
    values = {"school_one":data["school"],"school_two":data["school"],
              "debater_one":data["one"],"debater_two":data["two"],"keep_debater":"debater_one"}
    rejected = submit(create,{**values,"debater_two":values["debater_one"]})
    assert "Debaters must be different." in rejected["content"]
    created = submit(create,values)
    assert created["redirected"] and "Merge request submitted." in created["content"], created
    duplicate = submit(create, values)
    assert "A pending merge request already exists" in duplicate["content"]
    open_page(django,review,admin)
    request_id = driver.execute_script("""
        const pending = [...document.querySelectorAll("tr")].find(
            row => row.textContent.includes("Merge Alpha") && row.textContent.includes("Merge Zeta"));
        return pending.querySelector('input[name="request_id"]').value;
    """)

    open_page(rust,review,admin)
    swapped = submit(review,{"request_id":request_id,"action":"swap"})
    assert "Debaters swapped successfully." in swapped["content"]
    approved = submit(review,{"request_id":request_id,"action":"approve"})
    assert "Merged Merge Alpha into Merge Zeta." in approved["content"], approved
    open_page(django,review,admin)
    assert "Merge Zeta" in driver.page_source and "Approved" in driver.find_element("tag name","body").text
    db = Path(os.environ["BR_STATE"]) / "dev.sqlite3"
    with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT id FROM core_debater WHERE id=?",(data["one"],)).fetchone() is None
        assert connection.execute("SELECT status,primary_debater_id,secondary_debater_id FROM core_mergedebaterrequest WHERE id=?",
                                  (request_id,)).fetchone() == ("approved",data["two"],None)
    driver.delete_all_cookies()


def assert_mittab_dashboard(driver, django, rust):
    path = "/core/mittab-dashboard/"
    session = django_session()
    snapshots = []
    for server in (django, rust):
        driver.get(f"http://{HOST}:{server.port}{path}")
        driver.add_cookie({"name": "sessionid", "value": session})
        driver.get(f"http://{HOST}:{server.port}{path}")
        snapshots.append(driver.execute_script("""
            return [...document.querySelectorAll('.mittab-table tbody tr')].map(row =>
                [row.querySelector('strong').textContent, row.querySelector('a').href]);
        """))
    expected = [["North", "http://north.nu-tab.com/"], ["South", "https://south.example.test/"]]
    assert snapshots[0] == snapshots[1] == expected
    driver.delete_all_cookies()


def assert_synthetic_cleanup(driver, django, rust):
    code = """
from datetime import date
from django import setup
setup()
from core.models import Debater, School, Team, Tournament, TeamResult
import json
data = []
for label in ("Django", "Rust"):
    school = School.all_objects.create(name=f"Disposable {label} clean school", synthetic=True, temporary=True)
    debater = Debater.all_objects.create(first_name=f"Disposable {label}", last_name="Clean debater",
        school=school, synthetic=True)
    team = Team.objects.create(name=f"Disposable {label} clean team", synthetic=True)
    team.debaters.add(debater)
    canonical = School.objects.create(name=f"Disposable {label} retained school")
    source = School.all_objects.create(name=f"Disposable {label} source school", synthetic=True)
    referenced = Debater.all_objects.create(first_name=f"Disposable {label}", school=source, synthetic=True)
    other_team = Team.objects.create(name=f"Disposable {label} retained team", synthetic=True)
    other_team.debaters.add(referenced)
    event = Tournament.objects.create(manual_name=f"Disposable {label} referenced event",
        host=canonical, season="2026", date=date(2026, 9, 18))
    TeamResult.objects.create(tournament=event, team=other_team, place=1)
    data.append(dict(school=school.id,debater=debater.id,team=team.id,canonical=canonical.id,
        source=source.id,referenced=referenced.id,other_team=other_team.id))
print(json.dumps(data))
"""
    output = subprocess.check_output([str(ROOT / "venv/bin/python"), "-c", code],
        env={**os.environ, "SECRET_KEY": "disposable-local-parity-key"}, text=True)
    records = json.loads(output.splitlines()[-1])
    login_cookie = django_session()
    cleanup = "/core/admin/synthetic-cleanup/"
    tools = "/core/admin-tools/"
    resolve = "/core/admin/synthetic-resolve/"

    def admin_page(server, path):
        driver.get(f"http://{HOST}:{server.port}{path}")
        driver.add_cookie({"name": "sessionid", "value": login_cookie})
        driver.get(f"http://{HOST}:{server.port}{path}")

    def available():
        return driver.execute_script("""
            return [...document.querySelectorAll('input[name=selected_ids]')].map(input =>
                [input.value, input.closest('tr').innerText.replace(/\s+/g, ' ').trim()]);
        """)

    def post(path, values):
        return driver.execute_async_script("""
            const [path, values, done] = arguments;
            const body = new URLSearchParams(values);
            body.set('csrfmiddlewaretoken',document.querySelector('input[name=csrfmiddlewaretoken]').value);
            fetch(path,{method:'POST',body}).then(response =>
                done([response.status,new URL(response.url).pathname,response.redirected]), () => done([]));
        """, path, values)

    driver.delete_all_cookies()
    driver.get(f"http://{HOST}:{rust.port}{cleanup}")
    assert urlsplit(driver.current_url).path == "/auth/login/"
    admin_page(django, cleanup)
    original = available()
    admin_page(rust, cleanup)
    assert original == available()
    assert all(f"team:{row['team']}" in dict(original) for row in records)
    assert all(f"team:{row['other_team']}" not in dict(original) for row in records)

    db = Path(os.environ["BR_STATE"]) / "dev.sqlite3"
    for server, record in zip((django, rust), records, strict=True):
        admin_page(server, cleanup)
        assert post(cleanup, [["selected_ids", f"debater:{record['debater']}"]]) == [200,cleanup,True]
        with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as database:
            assert database.execute("SELECT id FROM core_debater WHERE id=?", (record["debater"],)).fetchone()
        admin_page(server, cleanup)
        selections = [["selected_ids",f"{name}:{record[name]}"] for name in ("school","debater","team")]
        selections.append(["selected_ids", f"team:{record['other_team']}"])
        assert post(cleanup, selections) == [200,cleanup,True]
        with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as database:
            for model in ("school", "debater", "team"):
                assert database.execute(f"SELECT id FROM core_{model} WHERE id=?", (record[model],)).fetchone() is None
            assert database.execute("SELECT id FROM core_team WHERE id=?", (record["other_team"],)).fetchone()
        admin_page(server, tools)
        values = [["entity_type","school"],["synthetic_id",str(record["source"])],
                  ["target_id",str(record["canonical"])],["reason","Disposable resolution parity"]]
        assert post(resolve, values) == [200,tools,True]
        with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as database:
            assert database.execute("SELECT school_id FROM core_debater WHERE id=?", (record["referenced"],)).fetchone() == (
                record["canonical"],)
            target, reason, context = database.execute(
                "SELECT resolved_to_id,reason,source_context FROM core_syntheticresolutionlog WHERE entity_type='school' AND synthetic_id=?",
                (record["source"],)).fetchone()
            assert (target, reason, json.loads(context)) == (
                record["canonical"],"Disposable resolution parity",{"source":"admin_tools"})
    driver.delete_all_cookies()


def assert_mittab_bundle_upload(driver, django, rust, fixture):
    event = fixture["bundle_event"]
    path = f"/core/tournaments/mittab_bundle/upload?tournament={event}"
    driver.delete_all_cookies()
    driver.get(f"http://{HOST}:{rust.port}{path}")
    assert urlsplit(driver.current_url).path == "/auth/login/"
    driver.add_cookie({"name": "sessionid", "value": django_session()})
    db = Path(os.environ["BR_STATE"]) / "dev.sqlite3"

    def snapshot():
        with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as connection:
            return connection.execute("""
                SELECT r.id, r.import_key, r.victor, r.stage, a.source_name, a.normalized_name,
                    j.original_name, j.is_chair, i.source_hash
                FROM core_round r JOIN core_importedroundmetadata m ON m.round_id=r.id
                JOIN core_debateralias a ON a.id=m.gov_1_alias_id
                JOIN core_importedroundjudge j ON j.round_metadata_id=m.id
                JOIN core_importedroundmetadata_sources s ON s.importedroundmetadata_id=m.id
                JOIN core_tournamentimport i ON i.id=s.tournamentimport_id
                WHERE r.tournament_id=? ORDER BY r.import_key
            """, (event,)).fetchall()

    def submit(server, document, name="fixture.json"):
        driver.get(f"http://{HOST}:{server.port}{path}")
        field = driver.find_element("id", "id_bundle_file")
        assert field.get_attribute("accept") == ".json,application/json"
        return driver.execute_async_script("""
            const [documentData, fileName, done] = arguments;
            const form = new FormData(document.querySelector('form[enctype]'));
            form.set('bundle_file', new Blob([documentData], {type:'application/json'}), fileName);
            fetch(location.pathname + location.search, {method:"POST", body:form}).then(async response => {
                const page = new DOMParser().parseFromString(await response.text(), "text/html");
                done({status:response.status, path:new URL(response.url).pathname, redirected:response.redirected,
                    message:page.querySelector('[role=status]')?.textContent.trim(),
                    error:page.querySelector('#error_1_id_bundle_file')?.textContent.trim()});
            }, () => done({status:0}));
        """, json.dumps(document, ensure_ascii=False), name)

    fixture_round = {
        "import_key": "prelim-1", "stage": "prelim", "division": "varsity", "round_number": 1, "label": "P1",
        "gov": {"debater_ids": [1, 2], "source_names": ["Grüße A", "Fixture B"]},
        "opp": {"debater_ids": [3, 4], "source_names": ["Fixture C", "Fixture D"]},
        "victor": 1, "judges": [{"original_name": "Disposable Judge", "is_chair": True}],
    }
    reference = {"schema_version": 1, "source": "mit_tab_black_rod_bundle", "tournament_name": "Disposable event",
                 "exported_at": "2026-09-20T23:00:00Z", "schools": [],
                 "debaters": [{"id": index+1, "apda_id": pk} for index, pk in enumerate(fixture["authors"])],
                 "rounds": [fixture_round, {**fixture_round, "round_number": 2, "label": "P2", "import_key": "prelim-2"}]}
    for server in (django, rust):
        bad_extension = submit(server, reference, name="fixture.txt")
        assert bad_extension["error"] == "Please upload a .json Mit-Tab bundle file.", bad_extension
    assert not snapshot()
    saved = submit(rust, reference)
    assert saved["redirected"] and saved["path"] == f"/core/tournaments/{event}", saved
    assert "Created 2 rounds" in saved["message"], saved
    before = snapshot()
    assert len(before) == 2 and before[0][4:8] == ("Grüße A", "grüsse a", "Disposable Judge", 1), before
    for server in (django, rust):
        duplicate = submit(server, reference)
        assert duplicate["error"] == "This exact Mit-Tab bundle was already uploaded for the selected tournament.", duplicate
    invalid = {**reference, "rounds": [{**fixture_round, "victor": 2},
                                      {**fixture_round, "import_key": "bad", "stage": "invalid"}]}
    failed = submit(rust, invalid)
    assert failed["error"] == "Round 'bad' has an unsupported stage." and snapshot() == before, failed
    skipped = {**reference, "rounds": [{**fixture_round, "opp": {}}]}
    answer = submit(rust, skipped)
    assert "Skipped 1 round with incomplete rosters." in answer["message"] and snapshot() == before, answer
    updated = {**reference, "rounds": [{**fixture_round, "victor": 2}]}
    result = submit(rust, updated)
    assert "updated 1 rounds, deleted 1 stale rounds." in result["message"], result
    after = snapshot()
    assert len(after) == 1 and after[0][:4] == (before[0][0], "prelim-1", 2, "prelim"), after
    driver.delete_all_cookies()


def assert_manual_results(driver, django, rust, fixture):
    manual = fixture["manual_event"]
    event = manual["id"]
    target = f"/core/tournaments/data_entry?tournament={event}"
    setup = f"/core/tournaments/data_entry/setup?tournament={event}"
    driver.delete_all_cookies()
    driver.get(f"http://{HOST}:{rust.port}{setup}")
    assert urlsplit(driver.current_url).path == "/auth/login/"
    driver.add_cookie({"name": "sessionid", "value": django_session()})
    root = f"http://{HOST}:{rust.port}"
    driver.get(root + setup)
    assert "Enter Results: " in driver.find_element("tag name", "h4").text
    field = driver.find_element("id", "id_api_url")
    assert field.get_attribute("placeholder") == "https://nu-tab.com/tournament/123"
    field.submit()
    WebDriverWait(driver, 10).until(lambda browser: browser.current_url == root + target)

    def form_state(server):
        driver.get(f"http://{HOST}:{server.port}{target}")
        return driver.execute_script("""
            return {
                tabs: [...document.querySelectorAll('#dataEntryTabs a')].map(link => link.textContent.trim()),
                team: [...document.querySelectorAll('#varsity_teams-table select')].map(select => select.value),
                speaker: [...document.querySelectorAll('#varsity_speakers-table select')].map(select => select.value),
                controls: [...document.querySelectorAll('.add-form-btn')].map(button => button.textContent.trim()),
            };
        """)
    reference, candidate = form_state(django), form_state(rust)
    assert reference == candidate, (reference, candidate)

    def send(fields):
        return driver.execute_async_script("""
            const [fields, done] = arguments;
            const body = new URLSearchParams(new FormData(document.querySelector('#data-entry-form')));
            Object.entries(fields).forEach(([name, value]) => body.set(name, value));
            fetch(location.pathname + location.search, {method:"POST", body}).then(async response => {
                const page = new DOMParser().parseFromString(await response.text(), "text/html");
                done({status:response.status, path:new URL(response.url).pathname, redirected:response.redirected,
                    error:page.querySelector('[role=alert]')?.textContent.trim()});
            }, () => done({status:0}));
        """, fields)

    database = Path(os.environ["BR_STATE"]) / "dev.sqlite3"
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as db:
        prior_awards = db.execute("SELECT * FROM core_speakerresult WHERE tournament_id=?", (event,)).fetchall()
        for server in (django, rust):
            driver.get(f"http://{HOST}:{server.port}{target}")
            malformed = send({"varsity_speakers-0-ORDER": "not-a-number"})
            assert not malformed["redirected"], (server.name, malformed)
            assert db.execute("SELECT * FROM core_speakerresult WHERE tournament_id=?", (event,)).fetchall() == prior_awards

    driver.find_element("id", "novice_speakers-tab").click()
    driver.find_element("css selector", '.add-form-btn[data-tab="novice_speakers"]').click()
    WebDriverWait(driver, 10).until(lambda browser: browser.find_element(
        "css selector", "#id_novice_speakers-0-speaker.select2-hidden-accessible"))
    assert driver.find_element("id", "id_novice_speakers-TOTAL_FORMS").get_attribute("value") == "1"
    rejected = send({"varsity_speakers-0-speaker": 9_000_000})
    assert "Varsity Speakers tab" in rejected["error"] and not rejected["redirected"], rejected
    committed = send({"varsity_speakers-0-speaker": manual["people"][2]})
    assert committed["redirected"] and committed["path"] == f"/core/tournaments/{event}", committed
    assert form_state(django) == reference
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as db:
        assert db.execute("""SELECT h.debater_id FROM core_teamresult_hidden_from_debaters h
            JOIN core_teamresult r ON r.id=h.teamresult_id WHERE r.tournament_id=?""", (event,)).fetchall() == [(manual["people"][0],)]
        assert db.execute("SELECT hidden_from_profile FROM core_speakerresult WHERE tournament_id=?", (event,)).fetchone() == (1,)
        assert db.execute("SELECT points FROM core_toty WHERE team_id=? AND season='2026'", (manual["team"],)).fetchone() == (13.0,)
        assert db.execute("SELECT points FROM core_soty WHERE debater_id=? AND season='2026'", (manual["people"][2],)).fetchone() == (13.0,)
        assert db.execute("SELECT points FROM core_coty WHERE school_id=? AND season='2026'", (manual["school"],)).fetchone() == (38.0,)
    with sqlite3.connect(database) as db:
        for season, historical in fixture["historical_events"].items():
            for server in (django, rust):
                db.execute("INSERT INTO core_coty (season,school_id,place,points,tied) VALUES (?,?,1,6,0)",
                           (season, manual["school"]))
                db.commit()
                driver.get(f"http://{HOST}:{server.port}/core/tournaments/data_entry?tournament={historical}")
                saved = send({"autoqual_debaters": manual["people"][2]})
                assert saved["redirected"], (server.name, season, saved)
                assert not db.execute("SELECT 1 FROM core_coty WHERE season=? AND school_id=?",
                                      (season, manual["school"])).fetchall()
                assert db.execute("SELECT debater_id FROM core_tournamentautoqual WHERE tournament_id=?",
                                  (historical,)).fetchall() == [(manual["people"][2],)]

    code = """
from datetime import date
from django import setup
setup()
from core.models import Debater, School, Team, TeamResult, Tournament
import json

schools = [School.objects.create(name=f"Historical parity school {index}") for index in range(3)]
people = [Debater.objects.create(first_name="Parity", last_name=f"Roster {index}", school=school)
          for index, school in enumerate(schools)]
team = Team.objects.create(name="Historical parity trio")
team.debaters.add(*people)
temp_school = School.all_objects.create(name="Historical parity temporary school", temporary=True)
temp_person = Debater.objects.create(first_name="Parity", last_name="Temporary", school=temp_school)
event = Tournament.objects.create(host=schools[0], season="2026", date=date(2026, 10, 1),
    manual_name="Roster parity event", num_teams=24)
TeamResult.objects.create(tournament=event, team=team, place=1)
historical = Tournament.objects.create(host=schools[0], season="2024", date=date(2024, 10, 1),
    manual_name="Historical temporary school event", qual_type=Tournament.YALE)
print(json.dumps((event.pk, team.pk, historical.pk, temp_person.pk, temp_school.pk)))
"""
    result = subprocess.check_output([str(ROOT / "venv/bin/python"), "-c", code], text=True)
    roster_event, team, historical, person, school = json.loads(result.splitlines()[-1])
    with sqlite3.connect(database) as db:
        for server in (django, rust):
            db.execute("DELETE FROM core_toty WHERE team_id=? AND season='2026'", (team,))
            db.commit()
            driver.get(f"http://{HOST}:{server.port}/core/tournaments/data_entry?tournament={roster_event}")
            saved = send({})
            assert saved["redirected"], (server.name, saved)
            assert db.execute("SELECT points FROM core_toty WHERE team_id=? AND season='2026'",
                              (team,)).fetchone() == (13.0,)
            driver.get(f"http://{HOST}:{server.port}/core/tournaments/data_entry?tournament={historical}")
            saved = send({"autoqual_debaters": person})
            assert saved["redirected"], (server.name, saved)
            assert db.execute("SELECT debater_id FROM core_tournamentautoqual WHERE tournament_id=?",
                              (historical,)).fetchall() == [(person,)]
            assert db.execute("SELECT 1 FROM core_coty WHERE school_id=? AND season='2024'",
                              (school,)).fetchone() is None
    driver.delete_all_cookies()


def shared_import_cache_keys():
    from hashlib import md5

    season = "2026"
    digest = md5(f"{season}:".encode(), usedforsecurity=False).hexdigest()
    widget = ":1:recent_results_widget_html"
    fragments = [f":1:template.cache.{board}.{digest}" for board in
                 ("toty", "soty", "coty", "noty", "online_quals")]
    return widget, fragments


def cached_import_keys(keys):
    host, port = os.environ["BLACK_ROD_PARITY_MEMCACHED_ADDR"].rsplit(":", 1)
    with socket.create_connection((host, int(port)), timeout=2) as client, client.makefile("rwb") as cache:
        cache.write(("get " + " ".join(keys) + "\r\n").encode())
        cache.flush()
        found = set()
        while (header := cache.readline()) != b"END\r\n":
            value, key, _, length = header.split()
            assert value == b"VALUE"
            cache.read(int(length))
            assert cache.read(2) == b"\r\n"
            found.add(key.decode())
        return found


def assert_reviewed_tournament_import(driver, django, rust, fixture, provider):
    root = f"http://{HOST}:{rust.port}"
    sources = "/core/tournaments/import/"
    driver.delete_all_cookies()
    driver.get(root + sources)
    assert urlsplit(driver.current_url).path == "/auth/login/"
    driver.add_cookie({"name": "sessionid", "value": django_session()})
    db = Path(os.environ["BR_STATE"]) / "dev.sqlite3"
    remote = f"http://{HOST}:{provider.server_port}"
    for server in (django, rust):
        driver.get(f"http://{HOST}:{server.port}{sources}")
        assert driver.find_element("tag name", "h1").text == "Import tournament results"
        assert remote in driver.find_element("tag name", "body").text
    driver.find_element("css selector", "form button.btn-primary").click()
    WebDriverWait(driver, 10).until(lambda browser: "#schools-pane" in browser.page_source)
    query = parse_qs(urlsplit(driver.current_url).query)
    event = int(query["tournament"][0])
    assert len(query["review"][0]) == 32 and query["api_url"] == [remote]
    assert event != fixture["manual_event"]["id"]
    if os.environ.get("BLACK_ROD_PARITY_MEMCACHED_ADDR"):
        rust_review = driver.current_url

        def imported_rows(prefix, field):
            return driver.execute_script("""
                const [prefix, field] = arguments;
                return [...document.querySelectorAll(`input[id^="id_${prefix}-"][id$="-${field}"]`)]
                    .map(input => input.value);
            """, prefix, field)

        driver.get(f"http://{HOST}:{django.port}{urlsplit(rust_review).path}?{urlsplit(rust_review).query}")
        reopened = (imported_rows("schools", "name"), imported_rows("debaters", "first_name"))
        assert "Incoming" in reopened[1], reopened
        driver.get(rust_review)
        assert reopened == (imported_rows("schools", "name"), imported_rows("debaters", "first_name"))

    school_input = next(field for field in driver.find_elements("css selector", 'input[id^=id_schools-][id$=-name]')
                        if field.get_property("value") == "Fixture School From MIT-Tab")
    driver.execute_script("arguments[0].value = 'Reviewed Fixture School';", school_input)
    assert any(field.get_property("value") == "Incoming" for field in
               driver.find_elements("css selector", 'input[id^=id_debaters-][id$=-first_name]'))

    shared = os.environ.get("BLACK_ROD_PARITY_MEMCACHED_ADDR")
    if shared:
        widget, fragments = shared_import_cache_keys()
        widget_page = Case("/core/widgets/recent-results/", accept="text/html")
        assert request(django, Case("/?season=2026", accept="text/html"))[0] == 200
        old_widget = request(django, widget_page)
        assert old_widget[0] == 200
        assert request(rust, widget_page)[2] == old_widget[2]
        assert cached_import_keys([widget, *fragments]) == {widget, *fragments}

    original_teams = provider.import_payload["debater_counts"]["teams"]
    provider.import_payload["debater_counts"]["teams"] = 99
    try:
        result = driver.execute_async_script("""
            const done = arguments[0], form = document.querySelector('#data-entry-form');
            fetch(location.pathname + location.search, {method:'POST', body:new URLSearchParams(new FormData(form))})
                .then(async response => done({status:response.status,path:new URL(response.url).pathname,
                    text:(await response.text()).slice(0,160)}), () => done({status:0}));
        """)
    finally:
        provider.import_payload["debater_counts"]["teams"] = original_teams
    assert result["status"] == 200 and result["path"] == f"/core/tournaments/{event}", result
    if shared:
        assert not cached_import_keys([widget, *fragments])
        assert request(rust, widget_page)[0] == 200
        assert cached_import_keys([widget]) == {widget}
        assert request(django, widget_page)[0] == 200
        assert request(django, Case("/?season=2026", accept="text/html"))[0] == 200
        assert cached_import_keys(fragments) == set(fragments)

    with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as database:
        assert database.execute("SELECT num_teams,num_novice_debaters FROM core_tournament WHERE id=?", (event,)).fetchone() == (24, 7)
        assert database.execute("SELECT COUNT(*) FROM core_teamresult WHERE tournament_id=?", (event,)).fetchone() == (1,)
        assert database.execute("SELECT COUNT(*) FROM core_speakerresult WHERE tournament_id=?", (event,)).fetchone() == (1,)
        assert database.execute("SELECT COUNT(*) FROM core_round WHERE tournament_id=? AND import_key='review-prelim-1'", (event,)).fetchone() == (1,)
        assert database.execute("SELECT COUNT(*) FROM core_tournamentimport WHERE tournament_id=? AND original_file_name='api-bundle.json'", (event,)).fetchone() == (1,)
        assert database.execute("SELECT temporary FROM core_school WHERE name='Reviewed Fixture School'").fetchone() == (0,)
        assert database.execute("""SELECT d.temporary,s.name FROM core_debater d JOIN core_school s ON s.id=d.school_id
            WHERE d.first_name='Incoming' AND d.last_name='Participant'""").fetchone() == (0, "Reviewed Fixture School")
    driver.get(f"http://{HOST}:{django.port}/core/tournaments/{event}")
    assert "Incoming Participant" in driver.find_element("tag name", "body").text
    if os.environ.get("BLACK_ROD_PARITY_MEMCACHED_ADDR"):
        driver.get(f"http://{HOST}:{django.port}{sources}")
        driver.find_element("css selector", "form button.btn-primary").click()
        WebDriverWait(driver, 10).until(lambda browser: "review" in parse_qs(urlsplit(browser.current_url).query))
        django_review = urlsplit(driver.current_url)
        driver.get(f"{root}{django_review.path}?{django_review.query}")
        assert driver.find_element("id", "id_varsity_teams-0-debater_one").get_attribute("value")
        driver.refresh()
        assert driver.find_element("id", "id_varsity_teams-0-debater_one").get_attribute("value")
    driver.delete_all_cookies()


def assert_tournament_audit(driver, django, rust, fixture):
    overview = "/core/admin/tournament-pipeline-audit/?incomplete_only=1&sort=total_pct&dir=desc"
    detail = f"/core/admin/tournament-audit/{fixture['bundle_event']}/"
    driver.get(f"http://{HOST}:{rust.port}{overview}")
    assert urlsplit(driver.current_url).path == "/auth/login/"
    driver.add_cookie({"name": "sessionid", "value": django_session()})

    def snapshot(server, path):
        driver.get(f"http://{HOST}:{server.port}{path}")
        return driver.execute_script("""
            return {
                heading: document.querySelector('.card-title').textContent.trim(),
                badges: [...document.querySelectorAll('.card-body .badge')].map(node => node.textContent.trim()),
                tables: [...document.querySelectorAll('table')].map(table => [...table.rows].map(row =>
                    [...row.cells].map(cell => cell.textContent.replace(/\\s+/g, ' ').trim()))),
            };
        """)

    for path in (overview, detail):
        reference, candidate = snapshot(django, path), snapshot(rust, path)
        assert reference["heading"] == candidate["heading"] and reference["badges"] == candidate["badges"], path
        for left, right in zip(reference["tables"], candidate["tables"]):
            for expected, observed in zip_longest(left, right):
                assert expected is not None and observed is not None, path
                if path == detail:
                    # The target Select2 control is served by different template adapters.
                    assert expected[:-1] == observed[:-1], (expected, observed)
                else:
                    assert expected == observed, (expected, observed)
    driver.delete_all_cookies()


def assert_tournament_audit_writes(driver, django, rust, fixture):
    root = f"http://{HOST}:{rust.port}"
    event = fixture["bundle_event"]
    target = fixture["manual_event"]["id"]
    prefix = f"/core/admin/tournament-audit/{event}/"
    driver.delete_all_cookies()
    driver.get(root + prefix)
    driver.add_cookie({"name": "sessionid", "value": django_session()})
    people = fixture["authors"]
    payload = {"stage": "prelim", "round_number": "1", "victor": "1", "source_round_name": "Round 1 from fixture",
               **{f"{slot}_debater":pk for slot,pk in zip(("gov_1","gov_2","opp_1","opp_2"),people)},
               **{f"{slot}_role":role for slot,role in zip(("gov_1","gov_2","opp_1","opp_2"),
                    ("PM","MG","LO","MO"))},
               "gov_1_speaks": "27.1250", "gov_1_ranks": "2.0000", "gov_1_source_name": "Fixture manual source"}
    db = Path(os.environ["BR_STATE"]) / "dev.sqlite3"
    amendments = db.with_name("round-amendments.json")
    previous = json.loads(amendments.read_bytes())["actions"] if amendments.exists() else []

    def post(page, path, inputs):
        driver.get(root + page)
        return driver.execute_async_script("""
            const [path, inputs, done] = arguments;
            const token = document.querySelector('input[name=csrfmiddlewaretoken]').value;
            const body = new URLSearchParams({...inputs, csrfmiddlewaretoken:token});
            fetch(path, {method:'POST',body}).then(async response => {
                const page = new DOMParser().parseFromString(await response.text(), "text/html");
                done({status:response.status,path:new URL(response.url).pathname, redirected:response.redirected,
                    errors:[...page.querySelectorAll('.text-danger')].map(node => node.textContent.trim())});
            }, () => done({status:0}));
        """, path, inputs)

    create = prefix + "round/new/"
    invalid = post(create, create, {**payload, "opp_1_debater": people[0]})
    assert "Each ballot slot must use a different debater." in invalid["errors"], invalid
    created = post(create, create, payload)
    assert created["redirected"] and created["path"] == prefix, created
    with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as database:
        round_id, import_key = database.execute("""SELECT id,import_key FROM core_round
            WHERE tournament_id=? AND import_origin='manual'""", (event,)).fetchone()
        assert import_key.startswith("manual-amendment-"), import_key
        assert database.execute("SELECT speaks,ranks,debater_role FROM core_roundstats WHERE round_id=? AND debater_id=?",
                                (round_id, people[0])).fetchone() == (27.125, 2, "PM")

    path = prefix + f"round/{round_id}/"
    driver.get(f"http://{HOST}:{django.port}{path}")
    assert driver.find_element("id", "id_gov_1_speaks").get_attribute("value") == "27.1250"
    driver.get(root + path)
    assert driver.find_element("id", "id_gov_1_speaks").get_attribute("value") == "27.1250"
    changed = post(path, path, {**payload, "stage":"outround", "outround_stage":"2"})
    assert changed["redirected"] and changed["path"] == prefix, changed
    with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as database:
        assert database.execute("SELECT round_label,elim_size FROM core_round WHERE id=?", (round_id,)).fetchone() == ("Final", 2)
        assert database.execute("SELECT COUNT(*) FROM core_roundstats WHERE round_id=? AND (speaks IS NOT NULL OR ranks IS NOT NULL OR debater_role IS NOT NULL)",
                                (round_id,)).fetchone() == (0,)
    source_key = "api-provenance-fixture"
    upload = "/core/admin/round-amendments/"
    document = {"rounds":{"update":[{"tournament_id":event,"import_key":import_key,
                                     "round_label":source_key}]}}

    def upload_amendment(value):
        driver.get(root + "/core/admin-tools/")
        return driver.execute_async_script("""
            const [data, done] = arguments;
            const form = document.querySelector('form[action="/core/admin/round-amendments/"]');
            const body = new FormData(form);
            body.set('amendment_file',new Blob([data],{type:'application/json'}),'fixture-amendments.json');
            fetch(form.action,{method:'POST',body}).then(async response => {
                const page = new DOMParser().parseFromString(await response.text(), "text/html");
                done({status:response.status,redirected:response.redirected,text:page.body.textContent});
            }, () => done({status:0}));
        """, json.dumps(value))

    failed = upload_amendment({"actions":[{"type":"update_round","round_id":round_id,"round_label":"rollback"},
                                        {"type":"update_round","round_id":99999999,"round_label":"missing"}]})
    assert not failed["redirected"] and "Round matching query does not exist" in failed["text"], failed["status"]
    with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as database:
        assert database.execute("SELECT round_label FROM core_round WHERE id=?", (round_id,)).fetchone() == ("Final",)
    assert upload_amendment(document)["redirected"]
    with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as database:
        assert database.execute("SELECT round_label FROM core_round WHERE id=?", (round_id,)).fetchone() == (source_key,)
        source = database.execute("""SELECT i.id FROM core_tournamentimport i
            JOIN core_importedroundmetadata_sources s ON s.tournamentimport_id=i.id
            WHERE i.tournament_id=? AND i.import_type='mittab_bundle'""", (event,)).fetchone()[0]
    moved = post(prefix, prefix + f"imports/{source}/move/", {f"import-{source}-target_tournament":target})
    assert moved["redirected"] and moved["path"] == prefix, moved
    destination = f"/core/admin/tournament-audit/{target}/"
    with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as database:
        assert database.execute("SELECT tournament_id FROM core_tournamentimport WHERE id=?", (source,)).fetchone() == (target,)
        assert database.execute("SELECT COUNT(*) FROM core_round WHERE tournament_id=? AND import_key='prelim-1'", (target,)).fetchone() == (1,)
    removed = post(destination, destination + f"imports/{source}/delete/", {})
    assert removed["redirected"] and removed["path"] == destination, removed
    deleted = post(prefix, path + "delete/", {})
    assert deleted["redirected"], deleted
    with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as database:
        assert database.execute("SELECT COUNT(*) FROM core_round WHERE id=?", (round_id,)).fetchone() == (0,)
        assert database.execute("SELECT COUNT(*) FROM core_round WHERE tournament_id=? AND import_key='prelim-1'", (target,)).fetchone() == (0,)
    actions = json.loads(amendments.read_bytes())["actions"]
    assert actions[:len(previous)] == previous
    appended = actions[len(previous):]
    assert [row["type"] for row in appended] == ["create_round","update_round","move_tournament_import",
        "delete_tournament_import","delete_round"], appended
    assert appended[0]["import_key"] == import_key
    driver.delete_all_cookies()


def assert_agent_reads(django, rust, fixture):
    base = "/api/agent/v1/"
    authorization = "Bearer " + fixture["agent"]
    cases = [
        Case(base), Case(base, authorization="Bearer br_agent_disposable_invalid"),
        Case(base, authorization=authorization), Case(base + "guide/", authorization=authorization),
        Case(base + "batches/", authorization=authorization),
        Case(base + "batches/", method="POST", body="{}", authorization=authorization),
    ]
    cases += [
        Case(base + f"records/{path}", authorization=authorization) for path in (
            "schools/?limit=3", f"schools/{fixture['role_school']}/", "schools/?synthetic=false&limit=3",
            f"debaters/{fixture['debater']}/", f"tournaments/{fixture['tournament']}/",
            "rounds/?limit=3", "round_stats/?limit=3", "round_metadata/?limit=3", "teams/?limit=3",
            "schools/?id__in=1,2,3", "schools/?q=roles", "schools/?name__gt=a", "unknown/",
        )
    ]
    compare_responses(django, rust, cases)


def assert_generated_codes(driver, django, rust, fixture):
    roots = [f"http://{HOST}:{server.port}" for server in (django, rust)]
    path = "/core/generated-codes/"
    driver.get(f"{roots[1]}/games/")
    driver.add_cookie({"name": "sessionid", "value": fixture["sessions"]["ordinary"]})
    seen = set()
    for sender, reader in ((1, 0), (0, 1)):
        driver.get(f"{roots[sender]}{path}")
        posted = driver.execute_async_script("""
            const done = arguments[0];
            const body = new URLSearchParams({
                "csrfmiddlewaretoken":document.querySelector("[name=csrfmiddlewaretoken]").value});
            fetch(location.pathname, {method:"POST", body, redirect:"manual"}).then(response =>
                done(response.type), () => done("error"));
        """)
        assert posted == "opaqueredirect", posted
        driver.get(f"{roots[reader]}{path}")
        code = driver.find_element("css selector", "code.d-block").text
        assert len(code) == 12 and code.isascii() and code.isalnum() and code == code.upper()
        assert code not in seen
        seen.add(code)
        driver.refresh()
        assert not driver.find_elements("css selector", "code.d-block")
    driver.delete_all_cookies()


def assert_admin_tools(driver, django, rust):
    roots = [f"http://{HOST}:{server.port}" for server in (django, rust)]
    driver.get(f"{roots[1]}/games/")
    driver.add_cookie({"name": "sessionid", "value": django_session()})
    states = []
    for root in roots:
        driver.get(f"{root}/core/admin-tools/")
        states.append(driver.execute_script("""
            const card = document.querySelector(".card-title").closest(".card-body");
            return Array.from(card.querySelectorAll("a, form")).map(node =>
                [node.textContent.trim(), node.getAttribute("href") || node.getAttribute("action")]);
        """))
    assert states[0] == states[1], (states[0], states[1])
    path = "/core/admin/school-short-names/"
    expected = directory_state(driver, django, path)
    actual = directory_state(driver, rust, path)
    assert expected == actual, (expected, actual)
    result = driver.execute_async_script("""
        const done = arguments[0];
        const form = document.querySelector("#short-name-form");
        const id = form.querySelector("#id_schools-0-id");
        if (!id) return done(null);
        const values = new URLSearchParams({
            "csrfmiddlewaretoken": form.querySelector("[name=csrfmiddlewaretoken]").value,
            "schools-TOTAL_FORMS": "1", "schools-INITIAL_FORMS": "1",
            "schools-0-id": id.value, "schools-0-short_name": "Parity abbreviation"
        });
        fetch(location.pathname, {method:"POST", body:values}).then(response =>
            done([id.value, response.status, response.redirected]), () => done(null));
    """)
    assert result and result[1:] == [200, True], result
    for root in roots:
        driver.get(f"{root}{path}")
        assert not driver.find_elements("css selector", f'input[name$="-id"][value="{result[0]}"]')
    driver.delete_all_cookies()


def assert_doty(driver, django, rust):
    driver.get(f"http://{HOST}:{rust.port}/games/")
    driver.add_cookie({"name": "sessionid", "value": django_session()})
    for season in ("2025", "2026", "invalid"):
        responses = []
        for server in (django, rust):
            driver.get(f"http://{HOST}:{server.port}/DOTY?season={season}")
            responses.append(driver.execute_script("""
                const text = (node) => node.textContent.replace(/\s+/g, " ").trim();
                return {
                    season: document.querySelector("#doty-season")?.value,
                    captions: Array.from(document.querySelectorAll("table caption")).map(text),
                    rows: Array.from(document.querySelectorAll("table tr")).map(row => Array.from(row.children).map(text)),
                    links: Array.from(document.querySelectorAll("table a")).map(link => link.getAttribute("href")),
                };
            """))
        assert responses[0]["captions"], season
        for key in responses[0]:
            expected, actual = responses[0][key], responses[1][key]
            if isinstance(expected, list) and isinstance(actual, list):
                assert len(expected) == len(actual), (season, key, len(expected), len(actual))
                for index, (left, right) in enumerate(zip(expected, actual)):
                    assert left == right, (season, key, index, left, right)
            else:
                assert expected == actual, (season, key, expected, actual)
    driver.delete_all_cookies()


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
            assert "Sign in to play." not in driver.find_element("tag name", "body").text
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


def import_payload(fixture):
    manual = fixture["manual_event"]
    incoming = {"debater_id": 1001, "apda_id": -1, "name": "Incoming Participant",
                "school_id": -1, "school_name": "Fixture School From MIT-Tab"}
    other = {"debater_id": 1002, "apda_id": manual["people"][0]}
    people = [incoming, other, {"debater_id": 1003, "apda_id": manual["people"][1]},
              {"debater_id": 1004, "apda_id": manual["people"][2]}]
    return {
        "schema_version": 1, "import_ready": True,
        "tournament": {"name":"Disposable MIT-Tab reviewed tournament", "date":"2026-12-01",
                       "season":"2026", "tournament_type":0, "host_school":{"apda_id":manual["school"]}},
        "varsity_team_placements": [[incoming, other]], "novice_team_placements": [], "non_placing_teams": [],
        "varsity_speaker_awards": [incoming], "novice_speaker_awards": [],
        "new_schools": ["Fixture School From MIT-Tab"], "new_debater_data": [incoming],
        "debater_counts": {"teams": 24, "novice": 7},
        "bundle": {"schema_version":1,"source":"mit_tab_black_rod_bundle","tournament_name":"Fixture",
            "schools":[{"id":100,"name":"Fixture School From MIT-Tab"}],
            "debaters":[{"id":row["debater_id"], "apda_id":row["apda_id"],
                "school_id":100 if index==0 else None, "name":row.get("name","Existing Person")}
                for index,row in enumerate(people)],
            "rounds":[{"import_key":"review-prelim-1","stage":"prelim","round_number":1,"victor":1,
                "gov":{"debater_ids":[1001,1002],"source_names":["Incoming Participant","Fixture A"]},
                "opp":{"debater_ids":[1003,1004],"source_names":["Fixture B","Fixture C"]}}],
        },
    }


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
        elif location.path == "/mittab-fixture":
            payload = b"""<div id='content'>
                <a href='/north'>&#110;orth.nu-tab.com</a>
                <a href='https://south.example.test/'> <b>south</b>.nu-tab.com </a>
                <a href='/ignored'>ignored.example.test</a>
            </div>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif location.path == "/api/tournaments":
            self.reply({"tournaments": [{"url": f"http://{HOST}:{self.server.server_port}"}]})
        elif location.path == "/api/import-results" and self.server.import_payload:
            self.reply(self.server.import_payload)
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
            self.reply(self.server.oauth_profile)
        else:
            self.send_error(404)

    def do_POST(self):
        values = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
        if self.path == "/oauth/token" and values.get("code") == ["fixture-code"]:
            self.reply({"access_token": "fixture-token", "expires_in": 600})
        else:
            self.send_error(400)


@contextmanager
def oauth_provider(fixture=None):
    with ThreadingHTTPServer((HOST, 0), OAuthProvider) as server:
        server.import_payload = import_payload(fixture) if fixture else None
        server.oauth_profile = {"ID":1234567,"user_nicename":"rustparity",
            "user_email":"oauth-fixture@example.test","display_name":"Example Tester",
            "user_roles":["private_side_viewer"]}
        thread = Thread(target=server.serve_forever, daemon=True)
        origin = f"http://{HOST}:{server.server_port}"
        os.environ["BLACK_ROD_OAUTH_BASE"] = f"{origin}/"
        os.environ["LIVE_MITTAB_DIRECTORY_URL"] = f"{origin}/api/tournaments"
        os.environ["NU_TAB_URL"] = f"{origin}/mittab-fixture"
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


def assert_oauth_roundtrip(driver, rust, django):
    root = f"http://{HOST}:{rust.port}"
    driver.get(f"{root}/accounts/apdaonline/login/callback/?code=fixture-code&state=unrecognized")
    assert "Third-Party Login Failure" in driver.find_element("tag name", "h1").text
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


def assert_oauth_pending_signup(driver, django, rust, provider):
    code = """
from django import setup
setup()
from allauth.account.models import EmailAddress
from core.models import User
user = User.objects.get(username="tab")
EmailAddress.objects.get_or_create(user=user, email="taken-oauth-fixture@example.test",
                                  defaults={"verified":True,"primary":False})
"""
    subprocess.run([str(ROOT / "venv/bin/python"), "-c", code], check=True, capture_output=True)

    def open_signup(source, replacement, case):
        provider.oauth_profile = {"ID":5678930 + case, "user_nicename":f"signupfixture{case}",
            "user_email":"taken-oauth-fixture@example.test","display_name":"Pending Fixture",
            "user_roles":[]}
        source_root = f"http://{HOST}:{source.port}"
        replacement_root = f"http://{HOST}:{replacement.port}"
        driver.get(source_root + "/robots.txt")
        driver.delete_all_cookies()
        driver.get(source_root + "/accounts/apdaonline/login/?next=/games/")
        driver.find_element("css selector", '.card form button[type="submit"]').click()
        path = "/accounts/3rdparty/signup/"
        try:
            WebDriverWait(driver, 10).until(lambda browser: browser.current_url == source_root + path)
        except TimeoutException as error:
            raise AssertionError(f"{source.name} did not open pending signup: {urlsplit(driver.current_url).path}") from error
        driver.get(replacement_root + path)
        assert driver.find_element("id", "id_username").get_attribute("value") == f"signupfixture{case}"
        assert driver.find_element("id", "id_email").get_attribute("value") == "taken-oauth-fixture@example.test"
        old = driver.find_element("tag name", "html")
        driver.find_element("id", "id_email").submit()
        WebDriverWait(driver, 10).until(document_replaced(old))
        assert "An account already exists with this email address." in driver.find_element("tag name", "body").text
        email = driver.find_element("id", "id_email")
        email.clear()
        email.send_keys(f"signup-fixture-{case}@example.test")
        email.submit()
        WebDriverWait(driver, 10).until(lambda browser: browser.current_url == replacement_root + "/games/")
        for root in (source_root, replacement_root):
            driver.get(root + "/games/")
            assert "My Account" in driver.find_element("tag name", "nav").get_attribute("textContent")
        driver.delete_all_cookies()

    open_signup(django, rust, 1)
    open_signup(rust, django, 2)


def assert_oauth_connections(driver, django, rust, provider):
    from selenium.webdriver.support.expected_conditions import staleness_of
    session = django_session()
    provider.oauth_profile = {
        "ID":2345678,"user_nicename":"connectionparity","user_email":"connection-fixture@example.test",
        "display_name":"Connected Fixture","user_roles":[],
    }
    route = "/accounts/3rdparty/"
    rust_root, django_root = [f"http://{HOST}:{server.port}" for server in (rust,django)]
    driver.get(rust_root + "/robots.txt")
    driver.delete_all_cookies()
    driver.add_cookie({"name":"sessionid","value":session})
    driver.get(rust_root + route)
    driver.find_element("link text","APDAOnline").click()
    assert urlsplit(driver.current_url).path == "/accounts/apdaonline/login/"
    assert "Sign In via APDAOnline" in driver.find_element("tag name","body").text
    driver.find_element("css selector",'.card form button[type="submit"]').click()
    WebDriverWait(driver,10).until(lambda browser: browser.current_url==rust_root+route)
    assert "The third-party account has been connected." in driver.find_element("tag name","body").text
    assert driver.get_cookie("sessionid")["value"] == session
    driver.get(django_root + route)
    assert "Connected Fixture" in driver.find_element("tag name","body").text
    driver.get(rust_root + route)
    driver.find_element("name","account").click()
    old = driver.find_element("tag name","html")
    driver.find_element("css selector",'main form button[type="submit"]').click()
    WebDriverWait(driver,10).until(document_replaced(old))
    assert "The third-party account has been disconnected." in driver.find_element("tag name","body").text
    driver.get(django_root + route)
    assert "Connected Fixture" not in driver.find_element("tag name","body").text
    driver.delete_all_cookies()


def assert_account_reauthentication(driver, django, rust):
    for server in (django, rust):
        origin = f"http://{HOST}:{server.port}"
        driver.get(origin + "/robots.txt")
        driver.delete_all_cookies()
        driver.add_cookie({"name": "sessionid", "value": django_session()})
        driver.get(origin + "/accounts/reauthenticate/?next=/games/")
        old = driver.find_element("tag name", "html")
        password = driver.find_element("id", "id_password")
        driver.execute_script("arguments[0].value = arguments[1];", password, "parity-invalid-password")
        password.submit()
        WebDriverWait(driver, 10).until(document_replaced(old))
        error = driver.find_element("id", "id_password_error").text
        assert error == "Incorrect password.", (server.port, error)
        password = driver.find_element("id", "id_password")
        driver.execute_script("arguments[0].value = arguments[1];", password, "password")
        password.submit()
        WebDriverWait(driver, 10).until(lambda browser: browser.current_url == origin + "/games/")
        assert "My Account" in driver.find_element("tag name", "nav").get_attribute("textContent")
    driver.delete_all_cookies()


def assert_login_roundtrip(driver, rust, django):
    root = f"http://{HOST}:{rust.port}"
    driver.get(f"{root}/auth/login/?next=/games/")
    driver.delete_cookie("sessionid")
    assert driver.find_element("id", "id_remember").is_selected()
    driver.find_element("id", "id_username").send_keys("TaB")
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
    assert "Sign in to play." in driver.find_element("tag name", "body").text


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    database = (Path(os.environ["BR_STATE"]) / "dev.sqlite3").resolve(strict=True)
    options = Options()
    options.add_experimental_option("prefs", {
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
        "profile.password_manager_leak_detection": False,
    })
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


def assert_live_standings(driver, django, rust):
    def compare(reference, candidate):
        assert any("Fixture First" in cell for row in reference["rows"] for cell in row)
        for key in reference:
            for index, (left, right) in enumerate(zip_longest(reference[key], candidate[key])):
                assert left == right, f"/live {key}[{index}] differed: {left!r} vs {right!r}"

    expected = directory_state(driver, django, "/live")
    compare(expected, directory_state(driver, rust, "/live"))
    address = os.environ.get("BLACK_ROD_PARITY_MEMCACHED_ADDR")
    if not address:
        return
    host, port = address.rsplit(":", 1)
    assert host == HOST
    with socket.create_connection((host, int(port)), timeout=2) as client:
        stream = client.makefile("rwb")
        for suffix in ("", ":stale", ":lock"):
            stream.write(f"delete :1:live-standings:v4:2026{suffix}\r\n".encode())
            stream.flush()
            assert stream.readline().strip() in (b"DELETED", b"NOT_FOUND")
    compare(expected, directory_state(driver, rust, "/live"))
    compare(expected, directory_state(driver, django, "/live"))


def browser_checks(database, options, output):
    fixture = seed_browser_cases()
    seed_elo()
    subprocess.run([str(ROOT / "venv/bin/python"), "manage.py", "update_index", "--verbosity", "0"],
                   check=True, capture_output=True)
    with oauth_provider(fixture) as provider, smtp_sink() as mail, servers(database) as (django, rust), webdriver.Chrome(options=options) as driver:
        expected_home = home_state(driver, django)
        result_home = home_state(driver, rust)
        assert expected_home["active"] == "soty"
        for key, result in expected_home.items():
            assert result_home[key] == result, f"Home page {key} differs: {result!r} vs {result_home[key]!r}"
        assert_session_nav(driver, (django, rust))
        assert_login_roundtrip(driver, rust, django)
        assert_account_reauthentication(driver, django, rust)
        assert_password_accounts(driver, django, rust, mail)
        assert_account_entry_and_email(driver, django, rust, mail)
        assert_oauth_roundtrip(driver, rust, django)
        assert_oauth_connections(driver, django, rust, provider)
        assert_oauth_pending_signup(driver, django, rust, provider)
        assert_directories(driver, django, rust)
        assert_legacy_standings(driver, django, rust)
        assert_partnership_paths(driver, django, rust)
        assert_partnership_stats(driver, django, rust)
        if os.environ.get("BLACK_ROD_PARITY_MEMCACHED_ADDR"):
            assert_shared_partnership_cache(driver, django, rust)
        assert_search(driver, django, rust)
        if os.environ.get("BLACK_ROD_PARITY_MEMCACHED_ADDR"):
            assert_search_reindex(rust)
        assert_video_permissions(driver, django, rust, fixture)
        assert_api_basic_auth(django, rust, fixture)
        assert_video_editor(driver, django, rust, fixture)
        assert_random_video(driver, django, rust, fixture)
        assert_recent_results(driver, django, rust)
        assert_motion_progress(driver, django, rust, fixture)
        assert_motion_editor(driver, django, rust, fixture)
        assert_motion_import(driver, django, rust, fixture)
        assert_tag_creation(driver, django, rust, fixture)
        assert_resource_permissions(driver, django, rust, fixture)
        assert_resource_editor(driver, django, rust, fixture)
        assert_grid(driver, django, rust, fixture)
        assert_grid_generator(driver, django, rust, fixture)
        assert_infinite_grid(driver, django, rust, fixture)
        assert_career_export(django, rust, fixture)
        assert_scheduling(driver, django, rust, fixture)
        assert_doty(driver, django, rust)
        assert_admin_tools(driver, django, rust)
        assert_native_site_admin(driver, django, rust)
        assert_admin_model_saves(driver, django, rust)
        assert_admin_preserves_linked_state(driver, django, rust)
        assert_admin_across_pages(driver, rust)
        assert_admin_topic_merges(driver, django, rust)
        assert_admin_speaker_visibility(driver, django, rust)
        assert_admin_attachments(driver, django, rust)
        assert_admin_formats(driver, django, rust)
        assert_debater_aliases(driver, django, rust)
        assert_native_round_inlines(driver, django, rust)
        assert_merge_suggestions(driver, django, rust)
        assert_speaks_statistics(driver, django, rust, fixture)
        assert_elo_dashboard(driver, django, rust, fixture)
        assert_synthetic_cleanup(driver, django, rust)
        assert_synthetic_suggestions(driver, django, rust)
        assert_mittab_dashboard(driver, django, rust)
        assert_mittab_bundle_upload(driver, django, rust, fixture)
        assert_tournament_audit(driver, django, rust, fixture)
        assert_tournament_audit_writes(driver, django, rust, fixture)
        assert_generated_codes(driver, django, rust, fixture)
        assert_entities(driver, django, rust, fixture)
        assert_school_admin(driver, django, rust, fixture)
        assert_merge_requests(driver, django, rust, fixture)
        assert_agent_reads(django, rust, fixture)
        assert_claims(driver, django, rust, fixture)
        assert_live_standings(driver, django, rust)
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
        assert_manual_results(driver, django, rust, fixture)
        assert_reviewed_tournament_import(driver, django, rust, fixture, provider)


if __name__ == "__main__":
    main()

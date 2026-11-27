"""Verify standings replay in Chrome against the same local Black Rod fixture."""

from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from html import unescape
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

from compare import Case, HOST, ROOT, compare as compare_responses, servers

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
from core.models.resource import Resource
from core.models.school import School
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
for label in ["ordinary", "account", "staff"]:
    user = get_user_model().objects.create_user(
        username=f"video-{label}", email=f"{label}@example.test", can_view_private_videos=label == "account",
    )
    if label == "staff":
        user.user_permissions.add(Permission.objects.get(content_type__app_label="core", codename="view_video"))
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
    assert "Sign in to play." in driver.find_element("tag name", "body").text


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
    fixture = seed_browser_cases()
    subprocess.run([str(ROOT / "venv/bin/python"), "manage.py", "update_index", "--verbosity", "0"],
                   check=True, capture_output=True)
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
        assert_legacy_standings(driver, django, rust)
        assert_partnership_paths(driver, django, rust)
        assert_partnership_stats(driver, django, rust)
        assert_search(driver, django, rust)
        assert_video_permissions(driver, django, rust, fixture)
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
        assert_speaks_statistics(driver, django, rust, fixture)
        assert_mittab_bundle_upload(driver, django, rust, fixture)
        assert_generated_codes(driver, django, rust, fixture)
        assert_entities(driver, django, rust, fixture)
        assert_school_admin(driver, django, rust, fixture)
        assert_agent_reads(django, rust, fixture)
        assert_claims(driver, django, rust, fixture)
        live_django = directory_state(driver, django, "/live")
        live_rust = directory_state(driver, rust, "/live")
        assert any("Fixture First" in cell for row in live_django["rows"] for cell in row)
        for key in live_django:
            for index, (left, right) in enumerate(zip_longest(live_django[key], live_rust[key])):
                assert left == right, f"/live {key}[{index}] differed: {left!r} vs {right!r}"
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

"""Compare both rendered HTML and desktop/mobile pixels against Django fixtures."""
from argparse import ArgumentParser
from contextlib import closing, contextmanager, nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import zip_longest
import json
import os
from pathlib import Path
from shutil import copyfile
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
from threading import Thread
from types import SimpleNamespace

from PIL import Image, ImageChops
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait

from browser import django_session, oauth_provider, seed_browser_cases, smtp_sink
from compare import HOST, ROOT, servers


VIEWPORTS = {"desktop": (1440, 1000), "mobile": (390, 844)}
PUBLIC = [
    ("home", "/", False),
    ("missing-page", "/fixture-route-does-not-exist/", False),
    ("missing-school", "/core/schools/999999999", False),
    ("replay", "/standings/replay/?season=2025&default=coty", False),
    ("schools", "/core/schools/", False),
    ("debaters", "/core/debaters/", False),
    ("teams", "/core/teams/", False),
    ("schedule", "/core/schedules/?season=2025", False),
    ("statistics", "/stats/", False),
    ("games", "/games/", False),
    ("api-schools", "/api/schools/?format=api", False),
    ("api-school", "/api/schools/7/detail/?format=api", False),
    ("api-school-encoded", "/api/schools/%37/detail/?format=api", False),
    ("api-standings", "/api/standings/?season=2025&default=toty&limit=2&format=api", False),
    ("api-agent-auth", "/api/agent/v1/?format=api", False),
    ("account-login", "/accounts/login/?next=/games/", False),
    ("account-login-code", "/accounts/login/code/confirm/", False),
    ("account-signup", "/accounts/signup/?next=/games/", False),
    ("account-logout", "/accounts/logout/?next=/games/", True),
    ("account-email", "/accounts/email/", True),
    ("account-confirm-email", "/accounts/confirm-email/invalid/", True),
    ("account-verify-email", "/accounts/confirm-email/", False),
    ("account-inactive", "/accounts/inactive/", False),
    ("account-connect", "/accounts/3rdparty/", True),
    ("account-social-error", "/accounts/3rdparty/login/error/", False),
    ("account-social-cancelled", "/accounts/3rdparty/login/cancelled/", False),
    ("account-reauth", "/accounts/reauthenticate/?next=/games/", True),
    ("account-provider", "/accounts/apdaonline/login/?next=/games/", False),
    ("login", "/auth/login/", False),
    ("legacy-password-change", "/auth/password_change/", True),
    ("legacy-password-change-done", "/auth/password_change/done/", True),
    ("legacy-password-reset", "/auth/password_reset/", False),
    ("legacy-password-reset-done", "/auth/password_reset/done/", False),
    ("legacy-password-reset-invalid", "/auth/reset/aW52YWxpZA/invalid/", False),
    ("legacy-password-reset-complete", "/auth/reset/done/", False),
    ("summernote-editor", "/summernote/editor/id-description/", False),
    ("school-profile", "/core/schools/7?season=2025", False),
    ("debater-profile", "/core/debaters/5270", False),
    ("team-profile", "/core/teams/6719", False),
    ("tournament-profile", "/core/tournaments/1203", False),
    ("tournament-profile-member", "/core/tournaments/1203", True),
    ("tournament-tab-cards", "/core/tournaments/862", False),
    ("tournament-tab-cards-member", "/core/tournaments/862", True),
    ("debater-profile-tab-cards", "/core/debaters/3683", True),
    ("tournaments", "/core/tournaments/", False),
    ("live", "/live", False),
    ("legacy-toty", "/core/toty?season=2025", False),
    ("legacy-soty", "/core/soty?season=2025", False),
    ("legacy-noty", "/core/noty?season=2019", False),
    ("legacy-coty", "/core/coty?season=2025", False),
    ("judges", "/core/dino-judges/", False),
    ("tournament-organizers", "/core/dino-tos/", False),
    ("search-form", "/search/", False),
    ("search-results", "/search/?q=Rory+Gillis", False),
    ("partnership-paths", "/core/debaters/partnership-paths/", False),
    ("partnership-network", "/core/debaters/partnership-paths/network/?from_season=2025&x_axis=first_year&y_axis=tournaments", False),
    ("partnership-stats", "/core/debaters/partnership-paths/stats/", False),
    ("partnership-person", "/core/debaters/partnership-paths/person/?debater=5270", False),
    ("motions", "/core/motions/", False),
    ("motion-detail", "/core/motions/1", False),
    ("videos", "/core/videos/", False),
    ("resources", "/core/resources/", False),
    ("resource-detail", "/core/resources/1", False),
    ("score-explainer", "/llm/oty-guide/", False),
    ("elo", "/core/elo/", False),
    ("doty", "/DOTY", False),
    ("daily-grid", "/immaculate-grid/", False),
    ("infinite-grid", "/immaculate-grid/infinite/", False),
    ("grid-leaderboard", "/immaculate-grid/leaderboard/", False),
    ("recent-results-widget", "/core/widgets/recent-results/", False),
    ("api-all-schools", "/api/schools/all/?format=api", False),
    ("api-schedule", "/api/schedule/?season=2025&format=api", False),
    ("api-replay", "/api/standings/replay/?season=2025&limit=2&format=api", False),
    ("api-through-date", "/api/standings/through-date/?season=2025&through=2026-01-01&limit=2&format=api", False),
    ("api-debater", "/api/debaters/5270/detail/?limit=2&format=api", False),
    ("api-team", "/api/teams/6719/detail/?limit=2&format=api", False),
    ("api-tournament", "/api/tournaments/1203/detail/?format=api", False),
    ("api-school-debaters", "/api/debaters/7/?format=api", False),
    ("api-oty-guide", "/api/oty-guide/?format=api", False),
]
ADMIN = [
    ("admin-login", "/admin/login/", False),
    ("admin-password", "/admin/password_change/", True),
    ("admin-password-done", "/admin/password_change/done/", True),
    ("admin-index", "/admin/", True),
    ("admin-core-index", "/admin/core/", True),
    ("admin-school-list", "/admin/core/school/?q=Harvard", True),
    ("admin-school-add", "/admin/core/school/add/", True),
    ("admin-school-import", "/admin/core/school/import/", True),
    ("admin-school-export", "/admin/core/school/export/?q=Harvard", True),
    ("admin-school-change", "/admin/core/school/7/change/", True),
    ("admin-school-history", "/admin/core/school/7/history/", True),
    ("admin-user-list", "/admin/core/user/", True),
    ("admin-tournament-list", "/admin/core/tournament/?season=2025", True),
    ("admin-user-add", "/admin/core/user/add/", True),
    ("admin-credential-list", "/admin/core/agentcredential/", True),
    ("admin-credential-add", "/admin/core/agentcredential/add/", True),
]

def staff_cases(fixture):
    manual = fixture["manual_event"]["id"]
    bundle = fixture["bundle_event"]
    motion = fixture["motion"]
    video = fixture["videos"][0]
    resource = fixture["resources"][0]
    return [
        ("staff-admin-tools", "/core/admin-tools/", True),
        ("staff-scheduling", "/core/admin/scheduling/", True),
        ("staff-tournament-audit", f"/core/admin/tournament-audit/{manual}/", True),
        ("staff-tournament-audit-round", f"/core/admin/tournament-audit/{manual}/round/new/", True),
        ("staff-pipeline-audit", "/core/admin/tournament-pipeline-audit/?incomplete_only=1&sort=total_pct&dir=desc", True),
        ("staff-speaks-stats", "/core/admin/speaks-stats/", True),
        ("staff-elo", "/core/elo/", True),
        ("staff-rankings", "/core/rankings-recompute/", True),
        ("staff-round-amendment", "/core/admin/round-amendments/", True),
        ("staff-mittab", "/core/mittab-dashboard/", True),
        ("staff-import", "/core/tournaments/import/", True),
        ("staff-import-bundle", f"/core/tournaments/mittab_bundle/upload?tournament={bundle}", True),
        ("staff-results-entry", f"/core/tournaments/data_entry?tournament={manual}", True),
        ("staff-results-setup", f"/core/tournaments/data_entry/setup?tournament={manual}", True),
        ("staff-motion-create", "/core/motions/create", True),
        ("staff-motion-update", f"/core/motions/{motion}/edit", True),
        ("staff-motion-import", "/core/motions/import", True),
        ("staff-video-create", "/core/videos/create", True),
        ("staff-video-update", f"/core/videos/{video}/edit", True),
        ("staff-video-delete", f"/core/videos/{video}/delete", True),
        ("staff-resource-create", "/core/resources/create", True),
        ("staff-resource-update", f"/core/resources/{resource}/edit", True),
        ("staff-resource-delete", f"/core/resources/{resource}/delete", True),
        ("staff-tournament-create", "/core/tournaments/create", True),
        ("staff-tournament-edit", f"/core/tournaments/{manual}/edit", True),
        ("staff-school-roles", "/core/manage-school-admins/", True),
        ("staff-school-dashboard", "/core/school-admin/", True),
        ("member-school-dashboard", "/core/school-admin/", "school_admin"),
        ("member-school-roster", f"/core/school-admin/{fixture['managed_school']}/debaters/", "school_admin"),
        ("member-school-roster-missing", "/core/school-admin/999999/debaters/", "school_admin"),
        ("member-school-roster-page-missing", f"/core/school-admin/{fixture['managed_school']}/debaters/?page=2", "school_admin"),
        ("member-school-roster-forbidden", f"/core/school-admin/{fixture['managed_school']}/debaters/", "ordinary"),
        ("staff-school-short-names", "/core/admin/school-short-names/", True),
        ("staff-synthetic-cleanup", "/core/admin/synthetic-cleanup/", True),
        ("staff-synthetic-suggestions", "/core/admin/synthetic-resolution-suggestions/", True),
        ("staff-merge-suggestions", "/core/admin/merge-suggestions/", True),
        ("staff-merge-review", "/core/merge-debaters/review/", True),
        ("staff-aliases", "/core/admin/debater-aliases/", True),
        ("staff-claim-review", "/core/claim-debater/review/", True),
        ("member-generated-codes", "/core/generated-codes/", "ordinary"),
        ("member-resource-forbidden", f"/core/resources/{fixture['resources'][1]}", "ordinary"),
        ("member-video-allowed", f"/core/videos/{fixture['videos'][1]}", "account"),
        ("member-video-forbidden", f"/core/videos/{fixture['videos'][2]}", "ordinary"),
        ("member-profile", "/core/my-profile/", "ordinary"),
        ("member-claim", "/core/claim-debater/request/", "ordinary"),
        ("member-owned-resources", "/core/resources/my-resources", "ordinary"),
        ("member-admin-forbidden", "/admin/core/user/", "ordinary"),
    ]


def admin_models(database):
    metadata = json.loads(Path(__file__).resolve().parents[1].joinpath("schema/admin.json").read_text())
    cases = []
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
        for app in metadata["apps"]:
            for model in app["models"]:
                application, slug = model["key"].split(".")
                route = f"/admin/{application}/{slug}/"
                prefix = f"admin-model-{application}-{slug}"
                cases.extend([
                    (f"{prefix}-list", route, True),
                    (f"{prefix}-add", route + "add/", True),
                ])
                table = model["table"]
                saved = connection.execute(f'SELECT id FROM "{table}" ORDER BY id LIMIT 1').fetchone()
                if saved:
                    if model["key"] == "core.user":
                        cases.append(("admin-model-core-user-password", f"{route}{saved[0]}/password/", True))
                    record = f"{route}{saved[0]}/"
                    cases.extend((f"{prefix}-{view}", record + view + "/", True)
                                 for view in ("change", "history", "delete"))
    return cases


DOM = r"""
const ignored = new Set(Array.from(arguments[0]));
function node(element) {
    if (element.nodeType === Node.COMMENT_NODE) return null;
    if (element.nodeType === Node.TEXT_NODE) {
        if (element.parentElement?.id === 'drf_csrf') {
            return element.textContent.replace(/("csrfToken":\s*")[^"]+/g, '$1<csrf>');
        }
        const verbatim = element.parentElement?.matches('pre, textarea, script, style');
        const raw = verbatim && element.textContent.trim() ? element.textContent : element.textContent.replace(/\s+/g, ' ').trim();
        return raw.replace(/((?:var csrftoken|window\.csrfToken) = ')[A-Za-z0-9]{64}(';)/g, "$1<csrf>$2") || null;
    }
    if (element.nodeType !== Node.ELEMENT_NODE) return null;
    const name = element.tagName.toLowerCase();
    if (ignored.has(name)) return null;
    const attrs = Array.from(element.attributes).map(item => {
        if (item.name === 'nonce') return [item.name, '<nonce>'];
        if (item.name === 'value' && element.getAttribute('name') === 'csrfmiddlewaretoken') {
            return [item.name, '<csrf>'];
        }
        let value = item.value.replaceAll(location.origin, '<origin>')
            .replace(/((?:(?:back|fore)Color(?:Picker|Palette)|note-dialog-(?:image|link|video)-[a-z-]+)-)\d{12,}/g, '$1<generated>');
        if (item.name === 'data-select2-id') {
            value = value.replace(/^(select2-data-\d+-)[a-z0-9]{4}$/, '$1<generated>');
        }
        if (item.name === 'class') value = value.split(/\s+/).filter(Boolean).sort().join(' ');
        return [item.name, value];
    }).sort((a,b) => a[0].localeCompare(b[0]));
    return [name, attrs, [...element.childNodes].map(node).filter(value => value !== null)];
}
return node(document.documentElement);
"""


def first_difference(left, right, path="document"):
    if left == right:
        return None
    if not isinstance(left, list) or not isinstance(right, list):
        return (path, left, right)
    if len(left) == 3 and len(right) == 3 and isinstance(left[0], str) and isinstance(right[0], str):
        path += "/" + left[0]
        if left[0] != right[0] or left[1] != right[1]:
            return (path, left[:2], right[:2])
        return first_difference(left[2], right[2], path)
    for index, (expected, actual) in enumerate(zip_longest(left, right)):
        if result := first_difference(expected, actual, f"{path}[{index}]"):
            return result
    return None


def open_page(driver, server, route, session):
    origin = f"http://{HOST}:{server.port}"
    driver.get(f"{origin}/robots.txt")
    driver.delete_all_cookies()
    driver.execute_script("localStorage.clear(); sessionStorage.clear();")
    if session:
        driver.add_cookie({"name": "sessionid", "value": session})
    driver.get(origin + route)
    driver.execute_async_script("""
        const callback = arguments[0];
        document.fonts.ready.then(() => requestAnimationFrame(() => requestAnimationFrame(callback)));
    """)


def capture(driver, server, route, session, target, exclude):
    open_page(driver, server, route, session)
    if route.startswith("/standings/replay/"):
        WebDriverWait(driver, 20).until(lambda browser: browser.execute_script("""
            const slider = document.getElementById("replay-slider");
            return slider && !slider.disabled && Number(slider.max) > 0;
        """))
        driver.execute_script("""
            const slider = document.getElementById("replay-slider");
            slider.value = slider.max;
            slider.dispatchEvent(new Event("input", {bubbles: true}));
        """)
        WebDriverWait(driver, 20).until(lambda browser: browser.execute_script("""
            return document.querySelector("#replay-coty-body tr")?.children.length >= 4;
        """))
    if route.startswith("/core/debaters/partnership-paths/network/"):
        WebDriverWait(driver, 30).until(lambda browser: browser.execute_script("""
            return document.querySelector("[data-network-motion]")?.textContent === "Axes fixed";
        """))
    if route == "/core/tournaments/862" and session and os.environ.get("ENABLE_TOURNAMENT_TAB_CARD_SEARCH") == "1":
        driver.execute_script("""
            const query = document.getElementById("teams");
            const first = document.querySelector("#team_list button");
            query.value = first.textContent.trim();
            query.dispatchEvent(new KeyboardEvent("keyup", {bubbles: true}));
        """)
        visible = [button for button in driver.find_elements("css selector", "#team_list button") if button.is_displayed()]
        assert len(visible) == 1, "The team-name filter should find one card"
        visible[0].click()
        WebDriverWait(driver, 10).until(lambda browser: browser.find_element("css selector", ".modal.show").is_displayed())
    driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 0, "y": 0})
    driver.execute_async_script("""
        const done = arguments[0];
        requestAnimationFrame(() => requestAnimationFrame(() => {
            const transitions = document.getAnimations().filter(animation => animation.effect?.getTiming().iterations !== Infinity);
            Promise.race([Promise.allSettled(transitions.map(transition => transition.finished)),
                          new Promise(resolve => setTimeout(resolve, 1500))])
                .then(() => requestAnimationFrame(() => requestAnimationFrame(done)));
        }));
    """)
    html = driver.execute_script(DOM, exclude)
    navigation = driver.execute_script("""
        return [location.pathname + location.search,
                performance.getEntriesByType("navigation")[0]?.responseStatus];
    """)
    target.with_suffix(".json").write_text(json.dumps(html, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    target.with_suffix(".html").write_text(driver.page_source, encoding="utf-8")
    sidebar = None
    if route == "/core/videos/create":
        # Sticky compositor layers can rasterize the same resting layout differently.
        # Capture the real layout and disable sticky painting only at identical bounds.
        sidebar = driver.execute_script("""
            const cards = [...document.querySelectorAll("#debater_create_form, #new_debaters")]
                .map(node => node.closest(".card.sticky-top")).filter(Boolean);
            const styles = cards.map(card => card.getAttribute("style"));
            const rects = () => cards.map(card => [card, ...card.querySelectorAll("*")]
                .map(node => node.getBoundingClientRect().toJSON()));
            const before = rects();
            const original = cards.map(card => {
                const css = getComputedStyle(card);
                return [css.position, css.top, css.zIndex];
            });
            for (const card of cards) {
                card.style.setProperty("position", "relative", "important");
                card.style.setProperty("top", "auto", "important");
            }
            const unchanged = JSON.stringify(before) === JSON.stringify(rects());
            if (!unchanged) cards.forEach((card, index) => {
                if (styles[index] === null) card.removeAttribute("style");
                else card.setAttribute("style", styles[index]);
            });
            return {bounds: before, styles: original};
        """)
        driver.execute_async_script("requestAnimationFrame(() => requestAnimationFrame(arguments[0]));")
    # The editing caret blinks independently of layout and Chrome's screenshot timing.
    # Leave focus and its styling intact, and save the original DOM above.
    driver.execute_script("""
        const field = document.activeElement;
        if (field?.matches("input, textarea, [contenteditable]")) {
            field.style.setProperty("caret-color", "transparent", "important");
        }
    """)
    # macOS's overlay scrollbar in a native multiple select can still be fading
    # after document animations finish. Capture the control in its resting state.
    driver.execute_async_script("""
        const done = arguments[0];
        const scrollingSelect = [...document.querySelectorAll('select[multiple]')]
            .some(select => select.clientHeight > 0 && select.scrollHeight > select.clientHeight);
        if (navigator.platform.startsWith('Mac') && scrollingSelect) {
            setTimeout(done, 1500);
        } else {
            done();
        }
    """)
    viewport = driver.execute_script("""
        const root = document.documentElement;
        return [root.clientWidth, root.clientHeight, root.scrollWidth, root.scrollHeight];
    """)
    assert driver.save_screenshot(str(target.with_suffix(".png")))
    return navigation, html, sidebar, viewport


# Permit isolated rasterization differences without relaxing the DOM comparison.
MAX_CHANGED_PIXELS = 2


def pixels(left, right, content_width=None):
    with Image.open(left.with_suffix(".png")) as expected, Image.open(right.with_suffix(".png")) as actual:
        difference = ImageChops.difference(expected.convert("RGB"), actual.convert("RGB"))
        if content_width is not None and content_width < difference.width:
            difference = difference.crop((0, 0, content_width, difference.height))
        maximum = ImageChops.lighter(ImageChops.lighter(*difference.split()[:2]), difference.split()[2])
        changed = maximum.point(lambda value: int(value > 8))
        count = changed.histogram()[1]
        return count, count / (expected.width * expected.height), changed.getbbox()


@contextmanager
def shared_origin():
    """Use one browser-visible origin for pages whose rendered bodies show absolute URLs."""
    class Proxy(BaseHTTPRequestHandler):
        def do_GET(self):
            with closing(proxy.target.connect()) as upstream:
                headers = {key: value for key, value in self.headers.items() if key.lower() != "connection"}
                upstream.request("GET", self.path, headers=headers)
                response = upstream.getresponse()
                body = response.read()
                self.send_response_only(response.status, response.reason)
                for key, value in response.getheaders():
                    if key.lower() not in {"connection", "transfer-encoding", "content-length"}:
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer((HOST, 0), Proxy) as proxy:
        thread = Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        try:
            yield proxy, SimpleNamespace(port=proxy.server_port)
        finally:
            proxy.shutdown()
            thread.join()


def compare(driver, django, rust, cases, sessions, output, viewports, exclude, pixel_only, proxy, origin, options):
    failures = []
    for mode in viewports:
        width, height = VIEWPORTS[mode]
        metrics = {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False}
        driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", metrics)
        for slug, route, authenticated in cases:
            session = sessions.get("admin" if authenticated is True else authenticated)
            reference = output / f"{mode}-{slug}-django"
            candidate = output / f"{mode}-{slug}-rust"
            for attempt in range(2):
                browser = nullcontext(driver) if attempt == 0 else webdriver.Chrome(options=options)
                with browser as active:
                    if attempt:
                        active.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", metrics)
                    proxy.target = django
                    left_navigation, left, left_sidebar, left_viewport = capture(
                        active, origin, route, session, reference, exclude
                    )
                    proxy.target = rust
                    right_navigation, right, right_sidebar, right_viewport = capture(
                        active, origin, route, session, candidate, exclude
                    )
                structural = first_difference(left, right)
                if left_navigation != right_navigation:
                    structural = ("navigation URL or status", left_navigation, right_navigation)
                if structural is None and left_sidebar != right_sidebar:
                    structural = ("video sidebar layout", left_sidebar, right_sidebar)
                content_width = None
                if route == "/stats/":
                    # Chrome can paint the native thumb a pixel early during chart layout.
                    # Compare the actual scroll extents and every pixel inside the viewport.
                    if left_viewport == right_viewport:
                        content_width = left_viewport[0]
                    elif structural is None:
                        structural = ("statistics viewport and scroll extent", left_viewport, right_viewport)
                count, fraction, difference = pixels(reference, candidate, content_width)
                if (structural is None and count <= MAX_CHANGED_PIXELS) or attempt == 1:
                    break
                print(f"{mode} {route} first comparison differed, repeating in a fresh Chrome session")
                for target in (reference, candidate):
                    for suffix in (".html", ".json", ".png"):
                        target.with_suffix(suffix).replace(target.with_name(target.name + "-initial").with_suffix(suffix))
            print(f"{mode} {route} DOM {'match' if structural is None else 'differs'}, changed pixels {fraction:.4%}")
            if structural is not None:
                location, expected, actual = structural
                print(f"  {location}\n  Django {repr(expected)[:200]}\n  Rust   {repr(actual)[:200]}")
            if (structural is not None and not pixel_only) or count > MAX_CHANGED_PIXELS:
                if difference:
                    print(f"  Changed pixel bounds {difference}")
                screenshots = output / "failures"
                screenshots.mkdir(exist_ok=True)
                for target in (reference, candidate):
                    for suffix in (".png", "-initial.png"):
                        image = target.with_name(target.name + suffix)
                        if image.is_file():
                            copyfile(image, screenshots / image.name)
                failures.append((mode, route, fraction, structural))
    return failures


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--group", choices=("public", "admin", "admin-models", "staff", "all"), default="all")
    parser.add_argument("--page", metavar="NAME", action="append")
    parser.add_argument("--viewport", choices=VIEWPORTS, action="append")
    parser.add_argument("--exclude-element", action="append", default=[])
    parser.add_argument("--pixel-only", action="store_true")
    arguments = parser.parse_args()
    output = arguments.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    viewports = arguments.viewport or list(VIEWPORTS)
    database = Path(os.environ["BR_STATE"]) / "dev.sqlite3"
    os.environ["BLACK_ROD_PARITY_PRODUCTION_ERRORS"] = "1"
    os.environ["BLACK_ROD_PARITY_STATIC"] = "1"
    os.environ["BLACK_ROD_PARITY_WRITABLE"] = "1"
    options = Options()
    # Software rasterization keeps identical native controls reproducible on both servers.
    for option in (
        "--headless=new", "--no-sandbox", "--disable-search-engine-choice-screen",
        "--disable-lcd-text", "--disable-gpu",
    ):
        options.add_argument(option)
    if os.environ.get("BLACK_ROD_PARITY_POSTGRES_URL"):
        parser.error("HTML parity uses a disposable local SQLite fixture.")
    with TemporaryDirectory(prefix="black-rod-html-parity-") as folder:
        local = Path(folder) / "dev.sqlite3"
        with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as source, sqlite3.connect(local) as copy:
            source.backup(copy)
        os.environ["BR_STATE"] = folder
        fixture = seed_browser_cases()
        model_cases = admin_models(local) if arguments.group in ("admin-models", "all") else []
        staff = staff_cases(fixture)
        cases = {"public": PUBLIC, "admin": ADMIN, "admin-models": model_cases, "staff": staff,
                 "all": [*PUBLIC, *ADMIN, *model_cases, *staff]}[arguments.group]
        if arguments.page:
            cases = [case for case in cases if case[0] in arguments.page]
        if not cases:
            parser.error("No matching pages in the selected group.")
        if any(route.startswith("/search/") for _, route, _ in cases):
            subprocess.run([str(ROOT / "venv/bin/python"), "manage.py", "update_index", "--verbosity", "0"],
                           check=True, capture_output=True)
        sessions = {"admin": django_session(), **fixture["sessions"]}
        with oauth_provider(fixture), smtp_sink(), servers(local) as (django, rust), webdriver.Chrome(options=options) as browser:
            with shared_origin() as (proxy, origin):
                failures = compare(browser, django, rust, cases, sessions, output, viewports,
                                   arguments.exclude_element, arguments.pixel_only, proxy, origin, options)
            if failures:
                raise SystemExit(f"{len(failures)} HTML or screenshot comparisons differed. See {output}.")


if __name__ == "__main__":
    main()

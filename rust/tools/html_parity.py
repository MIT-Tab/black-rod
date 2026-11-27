"""Compare both rendered HTML and desktop/mobile pixels against Django fixtures."""
from argparse import ArgumentParser
from itertools import zip_longest
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from PIL import Image, ImageChops
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

from browser import django_session, oauth_provider, seed_browser_cases, smtp_sink
from compare import HOST, servers


VIEWPORTS = {"desktop": (1440, 1000), "mobile": (390, 844)}
PUBLIC = [
    ("home", "/", False),
    ("replay", "/standings/replay/?season=2025&default=coty", False),
    ("schools", "/core/schools/", False),
    ("debaters", "/core/debaters/", False),
    ("teams", "/core/teams/", False),
    ("schedule", "/core/schedule/?season=2025", False),
    ("statistics", "/stats/", False),
    ("games", "/games/", False),
    ("login", "/auth/login/", False),
]
ADMIN = [
    ("admin-login", "/admin/login/", False),
    ("admin-index", "/admin/", True),
    ("admin-core-index", "/admin/core/", True),
    ("admin-school-list", "/admin/core/school/?q=Harvard", True),
    ("admin-school-add", "/admin/core/school/add/", True),
    ("admin-school-change", "/admin/core/school/7/change/", True),
]

DOM = r"""
const ignored = new Set(Array.from(arguments[0]));
function node(element) {
    if (element.nodeType === Node.COMMENT_NODE) return null;
    if (element.nodeType === Node.TEXT_NODE) {
        const verbatim = element.parentElement?.matches('pre, textarea, script, style');
        const raw = verbatim && element.textContent.trim() ? element.textContent : element.textContent.replace(/\s+/g, ' ').trim();
        return raw || null;
    }
    if (element.nodeType !== Node.ELEMENT_NODE) return null;
    const name = element.tagName.toLowerCase();
    if (ignored.has(name)) return null;
    const attrs = Array.from(element.attributes).map(item => {
        if (item.name === 'nonce') return [item.name, '<nonce>'];
        if (item.name === 'value' && element.getAttribute('name') === 'csrfmiddlewaretoken') {
            return [item.name, '<csrf>'];
        }
        let value = item.value.replaceAll(location.origin, '<origin>');
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
    html = driver.execute_script(DOM, exclude)
    target.with_suffix(".json").write_text(json.dumps(html, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    target.with_suffix(".html").write_text(driver.page_source, encoding="utf-8")
    assert driver.save_screenshot(str(target.with_suffix(".png")))
    return html


def pixels(left, right):
    with Image.open(left.with_suffix(".png")) as expected, Image.open(right.with_suffix(".png")) as actual:
        difference = ImageChops.difference(expected.convert("RGB"), actual.convert("RGB"))
        maximum = ImageChops.lighter(ImageChops.lighter(*difference.split()[:2]), difference.split()[2])
        changed = maximum.point(lambda value: int(value > 8)).histogram()[1]
        return changed / (expected.width * expected.height)


def compare(driver, django, rust, cases, session, output, viewports, exclude, pixel_only):
    failures = []
    for mode in viewports:
        width, height = VIEWPORTS[mode]
        driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
            "width":width, "height":height, "deviceScaleFactor":1, "mobile":False,
        })
        for slug, route, authenticated in cases:
            reference = output / f"{mode}-{slug}-django"
            candidate = output / f"{mode}-{slug}-rust"
            left = capture(driver, django, route, session if authenticated else None, reference, exclude)
            right = capture(driver, rust, route, session if authenticated else None, candidate, exclude)
            structural = first_difference(left, right)
            fraction = pixels(reference, candidate)
            print(f"{mode} {route} DOM {'match' if structural is None else 'differs'}, changed pixels {fraction:.4%}")
            if structural is not None:
                location, expected, actual = structural
                print(f"  {location}\n  Django {repr(expected)[:200]}\n  Rust   {repr(actual)[:200]}")
            if (structural is not None and not pixel_only) or fraction:
                failures.append((mode, route, fraction, structural))
    return failures


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--group", choices=("public", "admin", "all"), default="all")
    parser.add_argument("--page", metavar="NAME", action="append")
    parser.add_argument("--viewport", choices=VIEWPORTS, action="append")
    parser.add_argument("--exclude-element", action="append", default=[])
    parser.add_argument("--pixel-only", action="store_true")
    arguments = parser.parse_args()
    output = arguments.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    cases = {"public":PUBLIC, "admin":ADMIN, "all":[*PUBLIC, *ADMIN]}[arguments.group]
    if arguments.page:
        cases = [case for case in cases if case[0] in arguments.page]
    if not cases:
        parser.error("No matching pages in the selected group.")
    viewports = arguments.viewport or list(VIEWPORTS)
    database = Path(os.environ["BR_STATE"]) / "dev.sqlite3"
    os.environ["BLACK_ROD_PARITY_STATIC"] = "1"
    os.environ["BLACK_ROD_PARITY_WRITABLE"] = "1"
    options = Options()
    for option in ("--headless=new", "--no-sandbox", "--disable-search-engine-choice-screen"):
        options.add_argument(option)
    if os.environ.get("BLACK_ROD_PARITY_POSTGRES_URL"):
        parser.error("HTML parity uses a disposable local SQLite fixture.")
    with TemporaryDirectory(prefix="black-rod-html-parity-") as folder:
        local = Path(folder) / "dev.sqlite3"
        with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as source, sqlite3.connect(local) as copy:
            source.backup(copy)
        os.environ["BR_STATE"] = folder
        fixture = seed_browser_cases()
        session = django_session()
        with oauth_provider(fixture), smtp_sink(), servers(local) as (django, rust), webdriver.Chrome(options=options) as browser:
            failures = compare(browser, django, rust, cases, session, output, viewports,
                               arguments.exclude_element, arguments.pixel_only)
        if failures:
            raise SystemExit(f"{len(failures)} HTML or screenshot comparisons differed. See {output}.")


if __name__ == "__main__":
    main()

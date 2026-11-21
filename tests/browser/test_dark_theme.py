"""Check rendered dark-mode pages with axe-core's WCAG AA text contrast rule."""
import os
from pathlib import Path
import sys
import unittest

from selenium.webdriver.support.ui import WebDriverWait

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/skills/black-rod-task/scripts"))
from shot import login, make_driver  # noqa: E402


class DarkThemeTest(unittest.TestCase):
    def audit_pages(self, driver, pages):
        base = os.environ["BR_BROWSER_URL"]
        axe = (ROOT / "node_modules/axe-core/axe.min.js").read_text(encoding="utf-8")
        for path, expected_content in pages:
            with self.subTest(path=path):
                driver.get(base + path)
                self.assertEqual(driver.execute_script(
                    "return getComputedStyle(document.documentElement).colorScheme"
                ), "dark")
                WebDriverWait(driver, 10).until(lambda browser: browser.execute_script(
                    "return Boolean(document.querySelector(arguments[0]))", expected_content
                ))
                driver.execute_script(axe)
                result = driver.execute_async_script("""
                    const done = arguments[0];
                    axe.run({runOnly: ["color-contrast"], resultTypes: ["violations"]})
                       .then(({violations, passes}) => done({
                         checked: passes.length,
                         failures: violations.flatMap(rule => rule.nodes.map(node =>
                           `${node.target.join(" ")}: ${node.failureSummary}`
                         )),
                       }), error => done({error: String(error)}));
                """)
                self.assertNotIn("error", result)
                self.assertEqual(result["failures"], [], "\n".join(result["failures"]))
                self.assertGreater(result["checked"], 0, "No rendered text was checked")

    def test_dark_mode_and_text_contrast_desktop(self):
        driver = make_driver()
        self.addCleanup(driver.quit)
        base = os.environ["BR_BROWSER_URL"]
        driver.get(base + "/")
        driver.execute_script('localStorage.setItem("theme", "dark")')
        self.audit_pages(driver, [
            ("/?season=2025", "#toty"),
            ("/?season=2025&default=soty", "#soty.active"),
            ("/?season=2025&default=coty", "#coty.active"),
            ("/live", "#oty_pills"),
            ("/standings/replay/?season=2025", "#replay-slider:not([disabled])"),
            ("/core/toty?season=2025", ".table"),
            ("/core/soty?season=2025", ".table"),
            ("/core/coty?season=2025", ".table"),
            ("/core/noty?season=2024", ".table"),
            ("/stats/", "#debatersChart"),
            ("/games/", "a[href='/immaculate-grid/'].btn"),
            ("/core/schools/", ".table"),
            ("/core/debaters/", ".table"),
            ("/core/teams/", ".table"),
            ("/core/tournaments/", ".table"),
            ("/core/motions/", "form"),
            ("/core/resources/", ".card"),
            ("/core/videos/", "form"),
            ("/auth/login/", "input[name='username']"),
            ("/accounts/login/", "input[name='login']"),
            ("/search/?q=smith", ".card"),
            ("/core/widgets/recent-results/", ".widget"),
            ("/admin/login/", "#login-form"),
            ("/api/standings/", ".breadcrumb"),
            ("/llm/?endpoint=/api/schools/", ".standalone-page pre"),
            ("/llm/", ".standalone-page h1"),
        ])
        login(driver, base)
        self.audit_pages(driver, [
            ("/immaculate-grid/", ".immaculate-grid"),
            ("/immaculate-grid/leaderboard/", ".immaculate-grid"),
            ("/DOTY", "caption"),
            ("/core/admin-tools/", ".card"),
            ("/core/admin/scheduling/", ".card"),
            ("/core/schools/176/edit", ".card"),
            ("/core/debaters/1", ".card"),
            ("/core/tournaments/269", ".card"),
            ("/core/tournaments/import/", ".card"),
            ("/core/motions/create", "input[type='submit']"),
            ("/admin/", "#content-main"),
        ])

    def test_system_dark_mode_and_text_contrast_mobile(self):
        driver = make_driver(mobile=True)
        self.addCleanup(driver.quit)
        driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {
            "features": [{"name": "prefers-color-scheme", "value": "dark"}],
        })
        self.audit_pages(driver, [
            ("/?season=2025", "#toty"),
            ("/live", "#oty_pills"),
            ("/standings/replay/?season=2025", "#replay-slider:not([disabled])"),
        ])


if __name__ == "__main__":
    unittest.main()

"""Offline unit + determinism tests for DepPulse. No network required.

Run:  python -m unittest discover -s tests -v      (from the deppulse/ dir)
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import deppulse as dp  # noqa: E402

FIXTURES = os.path.join(ROOT, "fixtures")
TABLE = dp._load_table(dp.DEFAULT_TABLE)
DATA = os.path.join(HERE, "data")


def load(name):
    with open(os.path.join(DATA, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


class TestScan(unittest.TestCase):
    def test_node_app(self):
        scan = dp.cmd_scan(os.path.join(FIXTURES, "node-app"))
        names = {d["name"] for d in scan["deps"]}
        self.assertIn("openai", names)
        self.assertIn("wrangler", names)
        self.assertIn("@octokit/rest", names)
        self.assertIn("stripe", names)
        self.assertIn("npm", scan["ecosystems"])
        ctx_types = {c["type"] for c in scan["context"]}
        self.assertIn("github_actions", ctx_types)
        self.assertIn("dockerhub_image", ctx_types)

    def test_python_app(self):
        scan = dp.cmd_scan(os.path.join(FIXTURES, "python-app"))
        names = {d["name"] for d in scan["deps"]}
        self.assertIn("boto3", names)
        self.assertIn("anthropic", names)
        self.assertIn("datadog", names)
        self.assertIn("pypi", scan["ecosystems"])

    def test_go_docker_app(self):
        scan = dp.cmd_scan(os.path.join(FIXTURES, "go-docker-app"))
        ctx_types = {c["type"] for c in scan["context"]}
        self.assertIn("dockerhub_image", ctx_types)  # python:3.12-slim base image
        self.assertIn("go", scan["ecosystems"])

    def test_provider_sets_differ(self):
        """The whole premise: different repos yield different provider sets."""
        node = dp.cmd_map(dp.cmd_scan(os.path.join(FIXTURES, "node-app")), TABLE)
        py = dp.cmd_map(dp.cmd_scan(os.path.join(FIXTURES, "python-app")), TABLE)
        node_ids = {e["id"] for e in node["matched"]}
        py_ids = {e["id"] for e in py["matched"]}
        self.assertNotEqual(node_ids, py_ids)


class TestMap(unittest.TestCase):
    def test_node_matches(self):
        mp = dp.cmd_map(dp.cmd_scan(os.path.join(FIXTURES, "node-app")), TABLE)
        checked = {e["id"] for e in mp["matched"]}
        manual = {e["id"] for e in mp["manual"]}
        # JSON-leg providers we can check live:
        for pid in ("npm", "openai", "cloudflare", "github", "dockerhub"):
            self.assertIn(pid, checked, pid)
        # Stripe is a browser/manual-leg provider (no Statuspage summary.json):
        self.assertIn("stripe", manual)

    def test_reason_is_traceable(self):
        mp = dp.cmd_map(dp.cmd_scan(os.path.join(FIXTURES, "node-app")), TABLE)
        by_id = {e["id"]: e for e in mp["matched"]}
        self.assertIn("openai", by_id["openai"]["reason"])

    def test_coverage_identity(self):
        """detected == matched + no_status_page must ALWAYS hold (the coverage line cannot lie)."""
        for fx in ("node-app", "python-app", "go-docker-app"):
            mp = dp.cmd_map(dp.cmd_scan(os.path.join(FIXTURES, fx)), TABLE)
            c = mp["counts"]
            self.assertEqual(c["detected"], c["matched"] + c["no_status_page"], fx)

    def test_providers_filter(self):
        scan = dp.cmd_scan(os.path.join(FIXTURES, "node-app"))
        mp = dp.cmd_map(scan, TABLE, providers_filter="npm,openai")
        ids = {e["id"] for e in mp["matched"]} | {e["id"] for e in mp["manual"]}
        self.assertTrue(ids.issubset({"npm", "openai"}))

    def test_browser_flag_moves_leg(self):
        scan = dp.cmd_scan(os.path.join(FIXTURES, "node-app"))
        off = dp.cmd_map(scan, TABLE, use_browser=False)
        on = dp.cmd_map(scan, TABLE, use_browser=True)
        self.assertIn("stripe", {e["id"] for e in off["manual"]})
        self.assertIn("stripe", {e["id"] for e in on["matched"]})


class TestSummarize(unittest.TestCase):
    def test_operational(self):
        sev, detail, incidents = dp.summarize_summary_json(load("summary_operational.json"))
        self.assertEqual(sev, "operational")
        self.assertEqual(incidents, [])

    def test_incident(self):
        sev, detail, incidents = dp.summarize_summary_json(load("summary_incident.json"))
        self.assertEqual(sev, "partial_outage")
        self.assertEqual(detail, "Elevated 5xx errors on API")
        self.assertEqual(len(incidents), 1)

    def test_worst_of_indicator_and_component(self):
        # indicator none but a component in major_outage -> provider is major_outage
        summary = {"status": {"indicator": "none"},
                   "components": [{"name": "API", "status": "major_outage", "group": False}],
                   "incidents": []}
        sev, _, _ = dp.summarize_summary_json(summary)
        self.assertEqual(sev, "major_outage")


class TestComposeAndDeterminism(unittest.TestCase):
    def _fake(self):
        mapped = {
            "matched": [], "manual": [{"id": "stripe", "name": "Stripe",
                                       "status_url": "https://status.stripe.com", "reason": "stripe (package.json)"}],
            "counts": {"detected": 4, "matched": 3, "no_status_page": 1},
            "scanned_files": ["package.json", "package-lock.json"],
            "n_dependencies": 5, "table_version": "v1",
        }
        probed = {"statuses": [
            {"id": "npm", "name": "npm Registry", "status_url": "https://status.npmjs.org",
             "reason": "package-lock.json", "leg": "json", "severity": "operational", "detail": "", "incidents": []},
            {"id": "cloudflare", "name": "Cloudflare", "status_url": "https://www.cloudflarestatus.com",
             "reason": "wrangler (package.json)", "leg": "json", "severity": "partial_outage",
             "detail": "Elevated errors", "incidents": ["Elevated errors"]},
            {"id": "openai", "name": "OpenAI", "status_url": "https://status.openai.com",
             "reason": "openai (package.json)", "leg": "json", "severity": "degraded",
             "detail": "degraded: API", "incidents": []},
        ]}
        return mapped, probed

    def test_worst_first_ordering(self):
        mapped, probed = self._fake()
        board = dp.build_board(mapped, probed)
        sevs = [r["severity"] for r in board["rows"]]
        ranks = [dp.SEVERITY_RANK[s] for s in sevs]
        self.assertEqual(ranks, sorted(ranks))  # non-decreasing rank == worst-first
        self.assertEqual(board["rows"][0]["id"], "cloudflare")  # worst first

    def test_verdict_counts_problems(self):
        mapped, probed = self._fake()
        board = dp.build_board(mapped, probed)
        self.assertEqual(board["verdict"]["degraded"], 2)  # cloudflare + openai
        self.assertIn("2 provider(s) degraded", board["verdict"]["text"])

    def test_coverage_line_matches_counts(self):
        mapped, probed = self._fake()
        board = dp.build_board(mapped, probed)
        c = board["coverage"]
        self.assertEqual(c["detected"], c["checked"] + c["no_status_page"])

    def test_board_is_byte_stable(self):
        mapped, probed = self._fake()
        a = dp.render_board(dp.build_board(mapped, probed), use_emoji=False)
        b = dp.render_board(dp.build_board(mapped, probed), use_emoji=False)
        self.assertEqual(a, b)

    def test_all_green(self):
        mapped, probed = self._fake()
        for r in probed["statuses"]:
            r["severity"] = "operational"
        board = dp.build_board(mapped, probed)
        self.assertEqual(board["verdict"]["level"], "operational")
        self.assertIn("all systems green", board["verdict"]["text"])


if __name__ == "__main__":
    unittest.main()

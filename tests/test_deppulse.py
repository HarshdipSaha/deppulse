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
        # Every provider this repo touches is checkable live as of table v2.
        # Stripe used to be a browser-leg stub; it now has its own JSON parser.
        for pid in ("npm", "openai", "cloudflare", "github", "dockerhub", "stripe"):
            self.assertIn(pid, checked, pid)

    def test_matched_entries_carry_live_endpoint(self):
        """cmd_map is the only place that copies table fields into the entries
        probe_one actually reads. A field added to the table but never copied
        here is invisible to probe_one no matter what the CLI flag says --
        exactly the bug this test exists to catch."""
        mp = dp.cmd_map(dp.cmd_scan(os.path.join(FIXTURES, "node-app")), TABLE)
        by_id = {e["id"]: e for e in mp["matched"]}
        self.assertEqual(by_id["github"]["live_endpoint"], "https://api.github.com")
        self.assertIsNone(by_id["aws"]["live_endpoint"]) if "aws" in by_id else None

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

    def test_stripe_and_aws_are_probed_not_manual(self):
        """Table v2 promoted both off the browser stub, so they must land in
        'matched' with a parser attached, and nothing may sit in 'manual'."""
        scan = dp.cmd_scan(os.path.join(FIXTURES, "node-app"))
        mp = dp.cmd_map(scan, TABLE)
        by_id = {e["id"]: e for e in mp["matched"]}
        self.assertIn("stripe", by_id)
        self.assertEqual(by_id["stripe"]["parser"], "stripe_current")
        self.assertEqual(mp["manual"], [])
        self.assertEqual(mp["counts"]["no_status_page"], 0)

    def test_every_provider_has_a_probeable_leg(self):
        for provider in TABLE["providers"]:
            self.assertEqual(provider["leg"], "json", provider["id"])
            self.assertTrue(provider["summary_json_url"], provider["id"])


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


class TestShouldDirectCheck(unittest.TestCase):
    """--verify must only fire where it can teach us something: an
    "operational" status-page verdict, with --verify on, and a live_endpoint
    actually declared for that provider (AWS has none)."""

    def test_fires_only_when_all_three_hold(self):
        self.assertTrue(dp.should_direct_check("operational", True, "https://api.github.com"))

    def test_off_when_verify_flag_is_false(self):
        self.assertFalse(dp.should_direct_check("operational", False, "https://api.github.com"))

    def test_off_for_a_non_operational_verdict(self):
        self.assertFalse(dp.should_direct_check("degraded", True, "https://api.github.com"))
        self.assertFalse(dp.should_direct_check("partial_outage", True, "https://api.github.com"))
        self.assertFalse(dp.should_direct_check("unknown", True, "https://api.github.com"))

    def test_off_when_provider_has_no_live_endpoint(self):
        self.assertFalse(dp.should_direct_check("operational", True, None))

    def test_every_table_provider_except_aws_has_a_live_endpoint(self):
        for provider in TABLE["providers"]:
            if provider["id"] == "aws":
                self.assertNotIn("live_endpoint", provider)
            else:
                self.assertIn("live_endpoint", provider, provider["id"])


class TestVerifyRendering(unittest.TestCase):
    """The discrepancy note is additive: it must never change severity, must
    render only when unreachable, and must stay silent when --verify is off
    or the direct check succeeded (matches the status page)."""

    def _fake_operational(self, direct_check=None):
        mapped = {
            "matched": [], "manual": [],
            "counts": {"detected": 1, "matched": 1, "no_status_page": 0},
            "scanned_files": ["package.json"], "n_dependencies": 1, "table_version": "v3",
        }
        row = {"id": "github", "name": "GitHub", "status_url": "https://www.githubstatus.com",
               "reason": "@octokit/rest (package.json)", "leg": "json",
               "severity": "operational", "detail": "", "incidents": []}
        if direct_check is not None:
            row["direct_check"] = direct_check
        return mapped, {"statuses": [row]}

    def test_no_note_when_verify_never_ran(self):
        mapped, probed = self._fake_operational()
        out = dp.render_board(dp.build_board(mapped, probed), use_emoji=False)
        self.assertNotIn("(!)", out)

    def test_no_note_when_direct_check_succeeded(self):
        mapped, probed = self._fake_operational(
            {"url": "https://api.github.com", "reachable": True, "http_status": 200})
        out = dp.render_board(dp.build_board(mapped, probed), use_emoji=False)
        self.assertNotIn("(!)", out)

    def test_note_appears_when_direct_check_failed(self):
        mapped, probed = self._fake_operational(
            {"url": "https://api.github.com", "reachable": False, "error": "URLError"})
        board = dp.build_board(mapped, probed)
        out = dp.render_board(board, use_emoji=False)
        self.assertIn("(!)", out)
        self.assertIn("api.github.com", out)
        # The whole point: a discrepancy is a note, never a verdict change.
        self.assertEqual(board["verdict"]["level"], "operational")
        self.assertEqual(board["rows"][0]["severity"], "operational")


class TestDockerfileFrom(unittest.TestCase):
    """Real Dockerfiles (cal.com's, for one) put flags and stage aliases on FROM."""

    def test_platform_flag_is_not_the_image(self):
        imgs = dp._docker_images_from_dockerfile(
            "FROM --platform=$BUILDPLATFORM node:20-alpine AS builder\nRUN echo hi\n")
        self.assertEqual(imgs, ["node:20-alpine"])

    def test_stage_alias_is_not_treated_as_an_image(self):
        imgs = dp._docker_images_from_dockerfile(
            "FROM node:20 AS builder\nFROM builder\nFROM python:3.12-slim\n")
        self.assertEqual(imgs, ["node:20", "python:3.12-slim"])

    def test_scratch_and_args_still_skipped(self):
        imgs = dp._docker_images_from_dockerfile("FROM scratch\nFROM $BASE\nFROM redis:7\n")
        self.assertEqual(imgs, ["redis:7"])


class TestStripeParser(unittest.TestCase):
    def test_all_up(self):
        sev, detail, incs = dp.summarize_stripe_current(
            {"statuses": {"api": "up", "checkout": "up"}, "largestatus": "up",
             "message": "All services are online."})
        self.assertEqual(sev, "operational")
        self.assertEqual(detail, "All services are online.")
        self.assertEqual(incs, [])

    def test_one_service_degraded_is_named(self):
        sev, detail, incs = dp.summarize_stripe_current(
            {"statuses": {"api": "degraded", "checkout": "up"}, "largestatus": "up",
             "message": "All services are online."})
        self.assertEqual(sev, "degraded")
        self.assertIn("api", detail)
        self.assertTrue(incs)

    def test_overall_outage_wins(self):
        sev, _, _ = dp.summarize_stripe_current(
            {"statuses": {"api": "up"}, "largestatus": "down"})
        self.assertEqual(sev, "major_outage")

    def test_garbage_payload_is_unknown_not_a_crash(self):
        sev, _, _ = dp.summarize_stripe_current(["not", "a", "dict"])
        self.assertEqual(sev, "unknown")


class TestAwsParser(unittest.TestCase):
    def test_no_events_is_green(self):
        sev, detail, incs = dp.summarize_aws_currentevents([])
        self.assertEqual(sev, "operational")
        self.assertEqual(detail, "no active events")
        self.assertEqual(incs, [])

    def test_region_travels_with_the_detail(self):
        """A regional AWS event is not the reader's event unless they deploy
        there, so the region must be visible in the row."""
        sev, detail, incs = dp.summarize_aws_currentevents([
            {"summary": "Increased Error Rates", "service_name": "Multiple services",
             "region_name": "UAE"}])
        self.assertEqual(sev, "degraded")
        self.assertIn("UAE", detail)
        self.assertIn("Multiple services", detail)
        self.assertEqual(len(incs), 1)

    def test_disruption_outranks_error_rates(self):
        sev, _, incs = dp.summarize_aws_currentevents([
            {"summary": "Increased Error Rates", "region_name": "Ohio"},
            {"summary": "Service disruption", "region_name": "Tokyo"}])
        self.assertEqual(sev, "partial_outage")
        self.assertEqual(len(incs), 2)

    def test_unrecognised_active_event_stays_degraded(self):
        sev, _, _ = dp.summarize_aws_currentevents([{"summary": "Something odd"}])
        self.assertEqual(sev, "degraded")

    def test_garbage_payload_is_unknown_not_a_crash(self):
        sev, _, _ = dp.summarize_aws_currentevents({"not": "a list"})
        self.assertEqual(sev, "unknown")


class TestDecode(unittest.TestCase):
    """AWS serves UTF-16 with a BOM; everyone else serves UTF-8."""

    def test_utf16_be_bom(self):
        self.assertEqual(dp._decode(u'[{"a":1}]'.encode("utf-16-be", ) and b"\xfe\xff" + u'[]'.encode("utf-16-be")), "[]")

    def test_utf16_le_bom(self):
        self.assertEqual(dp._decode(b"\xff\xfe" + u'[]'.encode("utf-16-le")), "[]")

    def test_utf8_bom_is_stripped(self):
        self.assertEqual(dp._decode(b"\xef\xbb\xbf" + b'{"a":1}'), '{"a":1}')

    def test_plain_utf8(self):
        self.assertEqual(dp._decode(b'{"a":1}'), '{"a":1}')


class TestEncodingFallback(unittest.TestCase):
    """A Windows console/pipe on cp1252 cannot encode the traffic lights. The
    board must degrade to ASCII markers rather than die with UnicodeEncodeError."""

    class _Stream(object):
        def __init__(self, encoding, reconfigurable=False):
            self.encoding = encoding
            self._reconfigurable = reconfigurable

        def reconfigure(self, encoding=None, **kw):
            if not self._reconfigurable:
                raise OSError("not reconfigurable")
            self.encoding = encoding

    def test_cp1252_stream_refuses_emoji(self):
        self.assertFalse(dp.stdout_supports_emoji(self._Stream("cp1252")))

    def test_utf8_stream_accepts_emoji(self):
        self.assertTrue(dp.stdout_supports_emoji(self._Stream("utf-8")))

    def test_reconfigure_rescues_a_legacy_stream(self):
        stream = self._Stream("cp1252", reconfigurable=True)
        self.assertTrue(dp.stdout_supports_emoji(stream))
        self.assertEqual(stream.encoding, "utf-8")

    def test_ascii_board_renders_every_severity(self):
        board = {
            "verdict": {"level": "major_outage", "text": "1 provider(s) degraded"},
            "rows": [], "manual": [], "self": None,
            "coverage": {"checked": 0, "detected": 0, "no_status_page": 0,
                         "files": 0, "dependencies": 0},
            "table_version": "1",
        }
        for level in dp.SEVERITY_ORDER:
            board["verdict"]["level"] = level
            out = dp.render_board(board, use_emoji=False)
            out.encode("cp1252")  # must not raise
            self.assertIn("[%s]" % dp.LIGHT[level], out)


if __name__ == "__main__":
    unittest.main()

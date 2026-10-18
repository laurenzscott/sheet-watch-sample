"""Tests for watch.py. They never touch the network: the source is a
fake function that returns prepared CSV text."""

import email
import json
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import watch  # noqa: E402

CFG = {
    "source": {"name": "Test sheet", "url": "https://example.com/sheet.csv", "key": "id",
               "required_columns": ["id", "time", "mag", "place"]},
    "email": {"to": "client@example.com", "subject": "{count} new {rows} in {source}",
              "columns": ["time", "mag", "place"]},
    "state": {"remember_days": 7},
}

HEADER = "time,mag,place,id\n"
A = '2026-10-18T10:00:00Z,2.6,"10 km N of Somewhere, Alaska",ak1\n'
B = "2026-10-18T11:00:00Z,3.1,Off the coast,us2\n"
C = "2026-10-18T12:00:00Z,2.5,Inland,nc3\n"
D = "2026-10-18T13:00:00Z,4.0,Ridge,us4\n"
T0 = datetime(2026, 10, 18, 6, 0, tzinfo=timezone.utc)


def source(text):
    return lambda url: text


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.outbox = Path(self.tmp.name) / "outbox"

    def tearDown(self):
        self.tmp.cleanup()

    def run_once(self, text, hours=0, fetcher=None):
        return watch.run(CFG, self.data, self.outbox, now=T0 + timedelta(hours=hours),
                         fetcher=fetcher or source(text))

    def emails(self):
        return [email.message_from_bytes(p.read_bytes()) for p in sorted(self.outbox.glob("*.eml"))]


class TestRuns(Base):
    def test_first_run_is_a_baseline_and_sends_nothing(self):
        e = self.run_once(HEADER + A + B)
        self.assertEqual(e["status"], "baseline")
        self.assertEqual(e["email"], "none")
        self.assertEqual(self.emails(), [])
        seen = json.loads((self.data / "seen.json").read_text())["seen"]
        self.assertEqual(set(seen), {"ak1", "us2"})

    def test_new_rows_produce_one_email_with_attachment(self):
        self.run_once(HEADER + A + B)
        e = self.run_once(HEADER + A + B + C + D, hours=6)
        self.assertEqual((e["status"], e["new_rows"], e["email"]), ("ok", 2, "written"))
        mails = self.emails()
        self.assertEqual(len(mails), 1)
        m = mails[0]
        self.assertEqual(m["Subject"], "2 new rows in Test sheet")
        self.assertEqual(m["To"], "client@example.com")
        parts = list(m.walk())
        body = [p for p in parts if p.get_content_type() == "text/plain"
                and p.get_filename() is None][0].get_payload(decode=True).decode()
        self.assertIn("Inland", body)
        self.assertIn("Ridge", body)
        self.assertNotIn("Off the coast", body)
        att = [p for p in parts if p.get_filename() == "new-rows.csv"][0]
        csv_text = att.get_payload(decode=True).decode()
        self.assertEqual(csv_text.splitlines()[0], "time,mag,place,id")
        self.assertEqual(len(csv_text.splitlines()), 3)

    def test_unchanged_source_sends_nothing(self):
        self.run_once(HEADER + A + B)
        e = self.run_once(HEADER + A + B, hours=6)
        self.assertEqual((e["status"], e["new_rows"], e["email"]), ("ok", 0, "none"))
        self.assertEqual(self.emails(), [])

    def test_a_row_is_never_reported_twice(self):
        self.run_once(HEADER + A)
        self.run_once(HEADER + A + B, hours=6)
        e = self.run_once(HEADER + A + B, hours=12)
        self.assertEqual(e["new_rows"], 0)
        self.assertEqual(len(self.emails()), 1)

    def test_duplicate_key_inside_the_source_counts_once(self):
        self.run_once(HEADER + A)
        e = self.run_once(HEADER + A + B + B, hours=6)
        self.assertEqual(e["new_rows"], 1)

    def test_rows_that_left_the_source_window_are_not_new_again(self):
        self.run_once(HEADER + A + B)
        self.run_once(HEADER + B, hours=6)
        e = self.run_once(HEADER + A + B, hours=12)
        self.assertEqual(e["new_rows"], 0)


class TestSafeguards(Base):
    def test_missing_column_raises_an_alert_and_keeps_state(self):
        self.run_once(HEADER + A)
        before = (self.data / "seen.json").read_text()
        e = self.run_once("time,magnitude,place,id\n" + C, hours=6)
        self.assertEqual(e["status"], "source changed")
        self.assertIn("mag", e["note"])
        self.assertEqual((self.data / "seen.json").read_text(), before)
        self.assertTrue(self.emails()[0]["Subject"].startswith("Check needed"))

    def test_empty_source_after_baseline_is_an_alert_not_no_changes(self):
        self.run_once(HEADER + A)
        e = self.run_once(HEADER, hours=6)
        self.assertEqual(e["status"], "source empty")
        self.assertEqual(e["email"], "written")
        e = self.run_once(HEADER + A + C, hours=12)
        self.assertEqual(e["new_rows"], 1)

    def test_unreachable_source_raises_an_alert(self):
        self.run_once(HEADER + A)

        def broken(url):
            raise watch.FetchError("source not reachable after 3 attempts (timeout)")

        e = self.run_once("", hours=6, fetcher=broken)
        self.assertEqual(e["status"], "fetch failed")
        self.assertIn("not reachable", self.emails()[0].get_payload(decode=True).decode())


class TestFetch(unittest.TestCase):
    def test_a_refusal_is_never_retried(self):
        calls = []

        def opener(req, timeout):
            calls.append(req.get_header("User-agent"))
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

        with self.assertRaises(watch.FetchError) as ctx:
            watch.fetch("https://example.com/x.csv", wait=0, opener=opener)
        self.assertEqual(len(calls), 1)
        self.assertIn("403", str(ctx.exception))
        self.assertEqual(calls[0], watch.USER_AGENT)

    def test_server_errors_are_retried_three_times(self):
        calls = []

        def opener(req, timeout):
            calls.append(1)
            raise urllib.error.HTTPError(req.full_url, 503, "Unavailable", {}, None)

        with self.assertRaises(watch.FetchError):
            watch.fetch("https://example.com/x.csv", wait=0, opener=opener)
        self.assertEqual(len(calls), 3)


class TestHelpers(Base):
    def test_parse_handles_quoted_commas_and_blank_lines(self):
        header, rows = watch.parse(HEADER + A + "\n" + B)
        self.assertEqual(header, ["time", "mag", "place", "id"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["place"], "10 km N of Somewhere, Alaska")

    def test_old_keys_are_forgotten(self):
        seen = {"old": "2026-10-01T00:00:00Z", "new": "2026-10-17T00:00:00Z"}
        self.assertEqual(set(watch.forget_old(seen, T0, 7)), {"new"})

    def test_nothing_is_sent_without_smtp_settings(self):
        msg = watch.build_alert(CFG, "test", T0)
        self.assertFalse(watch.send_if_configured(msg, env={}))

    def test_outbox_keeps_only_the_newest_files(self):
        msg = watch.build_alert(CFG, "test", T0)
        for h in range(5):
            watch.write_outbox(msg, self.outbox, T0 + timedelta(hours=h), keep=3)
        names = [p.name for p in sorted(self.outbox.glob("*.eml"))]
        self.assertEqual(names, ["2026-10-18T0800Z.eml", "2026-10-18T0900Z.eml",
                                 "2026-10-18T1000Z.eml"])

    def test_every_run_is_logged_and_summed_up(self):
        self.run_once(HEADER + A)
        self.run_once(HEADER + A + B, hours=6)
        self.run_once(HEADER, hours=12)
        log = watch.read_run_log(self.data / "run-log.csv")
        self.assertEqual([e["status"] for e in log], ["baseline", "ok", "source empty"])
        facts = json.loads((self.data / "facts.json").read_text())
        self.assertEqual((facts["runs"], facts["runs_ok"], facts["runs_needing_a_check"]), (3, 2, 1))
        self.assertEqual((facts["new_rows_reported"], facts["emails"]), (1, 2))
        self.assertIn("status           source empty", (self.data / "last-run.txt").read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)

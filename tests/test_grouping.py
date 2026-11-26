import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hamlet.cli import main
from hamlet.grouping import body_text, group_corpus, ham_threads, spam_campaigns
from hamlet.store import SCHEMA_VERSION, Store


def message(identifier, body, extra=b""):
    return (b"Message-ID: <" + identifier.encode() + b"@example.invalid>\r\n"
            b"Subject: Same subject\r\n" + extra + b"\r\n" + body.encode())


class GroupingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "data"
        self.store = Store(self.root)
        self.uid = 0

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def add(self, raw, folder="INBOX", account="a"):
        self.uid += 1
        return self.store.add(account, folder, "7", self.uid, "2026-01-01", raw)

    def test_ham_missing_ancestor_links_siblings_but_subject_does_not(self):
        first = self.add(message("first", "one", b"References: <absent@example.invalid>\r\n"))
        second = self.add(message("second", "two", b"In-Reply-To: <absent@example.invalid>\r\n"))
        third = self.add(message("third", "three"))
        report = group_corpus(self.store)
        summary = report["accounts"]["a"]["ham_thread"]
        self.assertEqual(summary["messages"], 3)
        self.assertEqual(summary["groups"], 2)
        members = self.store.db.execute("""
            SELECT group_id,sha256 FROM group_members WHERE run_id=?
        """, (report["run_id"],)).fetchall()
        mapping = {row["sha256"]: row["group_id"] for row in members}
        self.assertEqual(mapping[first], mapping[second])
        self.assertNotEqual(mapping[first], mapping[third])

    def test_accounts_stay_separate_and_latest_review_wins(self):
        raw = message("shared", "one")
        digest = self.add(raw)
        self.add(raw, account="b")
        self.add(raw, folder="spam")
        report = group_corpus(self.store)
        self.assertEqual(report["excluded_unsure_messages"], 1)
        self.assertEqual(len(report["label_conflicts"]), 1)
        self.store.review("a", digest, "spam")
        self.store.review("a", digest, "ham")
        updated = group_corpus(self.store)
        self.assertEqual(updated["excluded_unsure_messages"], 0)
        self.assertEqual(updated["accounts"]["a"]["ham_thread"]["messages"], 1)
        self.assertEqual(updated["accounts"]["b"]["ham_thread"]["messages"], 1)
        group_ids = self.store.db.execute(
            "SELECT group_id FROM corpus_groups WHERE run_id=?", (updated["run_id"],)
        ).fetchall()
        self.assertEqual(len({row[0] for row in group_ids}), 2)
        self.assertNotEqual(report["run_id"], updated["run_id"])

    def test_spam_normalized_and_near_duplicate_templates_group(self):
        body = " ".join(f"distinctword{chr(97 + i // 26)}{chr(97 + i % 26)}" for i in range(100))
        self.add(message("one", body + " offer 1234"), folder="Junk")
        self.add(message("two", body + " offer 5678"), folder="Junk")
        self.add(message("three", body.replace("distinctwordaa", "replacement")), folder="Junk")
        unrelated = " ".join(f"unrelated{chr(97 + i // 26)}{chr(97 + i % 26)}" for i in range(100))
        self.add(message("four", unrelated), folder="Junk")
        report = group_corpus(self.store)
        summary = report["accounts"]["a"]["spam_campaign"]
        self.assertEqual(summary["groups"], 2)
        self.assertEqual(summary["largest_groups"][0]["size"], 3)
        self.assertGreaterEqual(summary["largest_groups"][0]["minimum_anchor_similarity"], 0.85)

    def test_no_transitive_similarity_chaining(self):
        rows = [{"account": "a", "sha256": name} for name in ("a", "b", "c")]
        fingerprints = [
            ("a", frozenset(range(100)), False),
            ("b", frozenset(range(1, 101)), False),
            ("c", frozenset(range(2, 102)), False),
        ]
        with patch("hamlet.grouping.body_text", return_value=("text", set())):
            with patch("hamlet.grouping.fingerprint", side_effect=fingerprints):
                with patch("hamlet.grouping.signature", return_value=(0,) * 32):
                    groups, _, _ = spam_campaigns(self.store, rows, 0.97)
        self.assertEqual([len(group) for group in groups], [2, 1])

    def test_short_generic_messages_are_not_merged(self):
        self.add(message("one", "click here"), folder="Junk")
        self.add(message("two", "click here"), folder="Junk")
        summary = group_corpus(self.store)["accounts"]["a"]
        self.assertEqual(summary["spam_campaign"]["groups"], 2)
        self.assertEqual(summary["spam_parsing_and_limits"]["insufficient_text"], 2)

    def test_html_and_charset_fallback_do_not_execute_content(self):
        raw = (b"Content-Type: text/html; charset=not-a-charset\r\n\r\n"
               b"<script>secret script</script><a href='https://example.invalid'>visible text</a>")
        digest = self.add(raw, folder="Junk")
        text, warnings = body_text(self.store.message_path(digest))
        self.assertIn("charset_fallback", warnings)
        self.assertIn("visible text", text)
        self.assertIn("https://example.invalid", text)
        self.assertNotIn("secret script", text)

    def test_truncated_body_is_not_given_a_confident_match(self):
        raw = message("one", "repeated word " * 50)
        self.add(raw, folder="Junk")
        self.add(message("two", "repeated word " * 50), folder="Junk")
        with patch("hamlet.grouping.MAX_TEXT_CHARS", 20):
            report = group_corpus(self.store)
        self.assertEqual(report["accounts"]["a"]["spam_campaign"]["groups"], 2)
        self.assertEqual(report["accounts"]["a"]["spam_parsing_and_limits"]["text_truncated"], 2)

    def test_report_is_reproducible_private_and_does_not_relabel(self):
        self.add(message("one", "private body"))
        before = self.store.db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        first = group_corpus(self.store)
        second = group_corpus(self.store)
        self.assertEqual(first, second)
        path = Path(first["report_path"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("private body", path.read_text())
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0], before)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM grouping_runs").fetchone()[0], 1)
        group_id = first["accounts"]["a"]["ham_thread"]["largest_groups"][0]["group_id"]
        for args in (["group-report"], ["group-show", group_id]):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["--data-root", str(self.root), "corpus", *args]), 0)
            json.loads(output.getvalue())

    def test_version_two_migrates_without_changing_corpus(self):
        digest = self.add(message("one", "body"))
        for table in ("group_members", "corpus_groups", "grouping_runs"):
            self.store.db.execute(f"DROP TABLE {table}")
        self.store.db.execute("PRAGMA user_version=2")
        self.store.close()
        self.store = Store(self.root)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        self.assertTrue(self.store.contains("a", "INBOX", "7", 1))
        self.assertEqual(self.store.message_path(digest).read_bytes(), message("one", "body"))


if __name__ == "__main__":
    unittest.main()

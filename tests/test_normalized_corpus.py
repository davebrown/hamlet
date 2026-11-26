import contextlib
from email import policy
from email.message import EmailMessage
import io
import json
from pathlib import Path
import tempfile
import unittest

from hamlet.cli import main
from hamlet.grouping import body_text, group_corpus
from hamlet.normalized import normalize_corpus
from hamlet.store import Store, content_identity


def original(identifier, body="short private original"):
    return (f"From: sender@example.invalid\r\nTo: receiver@example.invalid\r\n"
            f"Message-ID: <{identifier}@example.invalid>\r\n"
            f"Subject: original\r\n\r\n{body}\r\n").encode()


def report(*originals):
    wrapper = EmailMessage(policy=policy.SMTP)
    wrapper["From"] = "SpamAssassin <filter@example.invalid>"
    wrapper["Subject"] = "*****SPAM***** synthetic report"
    wrapper["X-Spam-Flag"] = "YES"
    wrapper["X-Spam-Checker-Version"] = "SpamAssassin 4.0"
    wrapper.set_content(
        "Spam detection software, running on the system example.invalid,\n"
        "has identified this incoming email as possible spam. The original\n"
        "message has been attached to this so you can view it or label\n"
        "similar future email. Content preview: synthetic\n"
        "Content analysis details: (8.0 points, 5.0 required)\n")
    for raw in originals:
        from email.parser import BytesParser
        wrapper.add_attachment(BytesParser(policy=policy.SMTP).parsebytes(raw))
    return wrapper.as_bytes()


class NormalizedCorpusTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "data"
        self.store = Store(self.root)
        self.uid = 0

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def add(self, raw, folder="Junk"):
        self.uid += 1
        return self.store.add("a", folder, "7", self.uid, "2026-01-01", raw)

    def test_preserves_sources_labels_and_records_versioned_provenance(self):
        raw = report(original("one"))
        digest = self.add(raw)
        self.store.review("a", digest, "spam")
        first = normalize_corpus(self.store)
        self.assertEqual(first["statuses"], {"unwrapped": 1})
        self.assertEqual(first, normalize_corpus(self.store))
        view = self.store.db.execute("SELECT * FROM normalized_messages").fetchone()
        self.assertNotEqual(view["effective_sha256"], digest)
        self.assertTrue(json.loads(view["steps"]))
        self.assertIn(b"short private original",
                      self.store.derived_path(view["effective_sha256"]).read_bytes())
        self.assertEqual(self.store.message_path(digest).read_bytes(), raw)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT label FROM reviews").fetchone()[0], "spam")
        self.assertNotIn("short private original", Path(first["report_path"]).read_text())
        self.assertEqual(self.store.derived_path(view["effective_sha256"]).stat().st_mode & 0o777,
                         0o600)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--data-root", str(self.root), "corpus", "verify"]), 0)
        self.assertEqual(json.loads(output.getvalue())["derived_checked"], 1)
        self.store.derived_path(view["effective_sha256"]).write_bytes(b"corrupt")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--data-root", str(self.root), "corpus", "verify"]), 1)
        with self.assertRaisesRegex(ValueError, "Derived integrity"):
            normalize_corpus(self.store)

    def test_report_boilerplate_does_not_merge_unrelated_originals(self):
        self.add(report(original("one", "alpha " * 50)))
        self.add(report(original("two", "omega " * 50)))
        result = group_corpus(self.store)
        self.assertEqual(result["normalization"]["statuses"], {"unwrapped": 2})
        self.assertEqual(result["accounts"]["a"]["spam_campaign"]["groups"], 2)

    def test_wrapper_and_raw_short_original_share_group_and_conflicts_are_excluded(self):
        raw = original("one")
        self.add(report(raw))
        digest = self.add(raw)
        result = group_corpus(self.store)
        self.assertEqual(result["accounts"]["a"]["spam_campaign"]["groups"], 1)
        self.assertEqual(result["distinct_safe_effective_messages"], 1)
        self.store.review("a", digest, "ham")
        result = group_corpus(self.store)
        self.assertEqual(len(result["effective_label_conflicts"]), 1)
        self.assertEqual(result["excluded_unsafe_or_effective_conflict_messages"], 2)
        self.assertEqual(result["accounts"]["a"]["spam_campaign"]["messages"], 0)

    def test_unwrapped_ham_uses_inner_reference_headers(self):
        self.add(report(original("ancestor")), "ham-sample")
        reply = original("reply").replace(
            b"Subject:", b"References: <ancestor@example.invalid>\r\nSubject:")
        self.add(reply, "ham-sample")
        result = group_corpus(self.store)
        self.assertEqual(result["accounts"]["a"]["ham_thread"]["groups"], 1)

    def test_ambiguous_reports_are_not_grouped_on_wrapper(self):
        self.add(report(original("one"), original("two")))
        result = group_corpus(self.store)
        self.assertEqual(result["normalization"]["statuses"], {"ambiguous": 1})
        self.assertEqual(result["excluded_unsafe_or_effective_conflict_messages"], 1)
        self.assertEqual(result["accounts"]["a"]["spam_campaign"]["messages"], 0)

    def test_forward_skips_entire_attached_message_subtree(self):
        forwarded = EmailMessage(policy=policy.SMTP)
        forwarded.set_content("ordinary outer text")
        from email.parser import BytesParser
        forwarded.add_attachment(BytesParser(policy=policy.SMTP).parsebytes(
            original("attached", "secret attached body")))
        digest = self.add(forwarded.as_bytes())
        normalized = normalize_corpus(self.store)
        self.assertEqual(normalized["statuses"], {"unchanged": 1})
        text, _ = body_text(self.store.message_path(digest))
        self.assertIn("ordinary outer text", text)
        self.assertNotIn("secret attached body", text)

    def test_export_normalizes_new_mail_and_repairs_unprocessed_resume(self):
        from unittest.mock import patch
        from hamlet.exporter import export
        from test_corpus import FakeIMAP

        raw = report(original("downloaded"))
        options = dict(
            store=self.store, host="example.invalid", username="test", password="secret",
            account="test", folders=["INBOX"], factory=FakeIMAP, progress=lambda _: None)
        with patch("test_corpus.RAW", raw):
            self.assertEqual(export(**options)[0]["downloaded"], 1)
            row = self.store.db.execute("SELECT * FROM normalized_messages").fetchone()
            self.assertEqual(row["status"], "unwrapped")
            self.assertEqual(self.store.message_path(row["source_sha256"]).read_bytes(), raw)
            self.store.db.execute("DELETE FROM normalized_messages")
            self.store.db.commit()
            self.assertEqual(export(**options)[0]["already_stored"], 1)
        self.assertEqual(self.store.db.execute(
            "SELECT status FROM normalized_messages").fetchone()[0], "unwrapped")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 1)

    def test_content_identity_handles_text_but_not_binary_wire_newlines(self):
        raw = original("text")
        self.assertEqual(content_identity(raw), content_identity(raw.replace(b"\r\n", b"\n")))
        for kind, encoding in (
                (b"text/plain", b"binary"),
                (b"application/octet-stream", b"8bit"),
                (b"application/octet-stream", b"quoted-printable"),
                (b"multipart/signed; boundary=example", b"7bit")):
            with self.subTest(kind=kind, encoding=encoding):
                binary = (b"From: sender@example.invalid\r\nContent-Type: " + kind
                          + b"\r\nContent-Transfer-Encoding: " + encoding
                          + b"\r\n\r\none\r\ntwo\r\n")
                self.assertNotEqual(content_identity(binary),
                                    content_identity(binary.replace(b"\r\n", b"\n")))

    def test_suspected_truncated_reports_are_excluded(self):
        raw = report(original("one"))
        raw = raw[:raw.rfind(b"--")]
        self.add(raw)
        result = group_corpus(self.store)
        self.assertEqual(result["normalization"]["statuses"], {"malformed": 1})
        self.assertEqual(result["excluded_unsafe_or_effective_conflict_messages"], 1)
        self.assertEqual(result["accounts"]["a"]["spam_campaign"]["messages"], 0)

    def test_verify_detects_broken_extraction_hash_chain(self):
        self.add(report(original("one")))
        normalize_corpus(self.store)
        row = self.store.db.execute("SELECT * FROM normalized_messages").fetchone()
        steps = json.loads(row["steps"])
        steps[0]["source_sha256"] = "0" * 64
        with self.store.db:
            self.store.db.execute("UPDATE normalized_messages SET steps=?",
                                  (json.dumps(steps),))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--data-root", str(self.root), "corpus", "verify"]), 1)
        verification = json.loads(output.getvalue())
        self.assertEqual(verification["normalized_checked"], 1)
        self.assertEqual(verification["failures"][0]["error"],
                         "normalization provenance mismatch")

    def test_schema_three_migration_preserves_sources(self):
        digest = self.add(original("one"))
        self.store.db.execute("DROP TABLE normalization_runs")
        self.store.db.execute("DROP TABLE normalized_messages")
        self.store.db.execute("PRAGMA user_version=3")
        self.store.close()
        self.store = Store(self.root)
        self.assertEqual(self.store.message_path(digest).read_bytes(), original("one"))
        self.assertTrue((self.root / "snapshots" / "pre-normalization-schema3.sqlite").exists())

import contextlib
import io
import imaplib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import ssl

from hamlet.cli import main
from hamlet.exporter import ExportError, credentials, export
from hamlet.store import SCHEMA_VERSION, Store


RAW = b"Message-ID: <synthetic@example.invalid>\r\nSubject: Test\r\n\r\nSynthetic mail.\r\n"


class FakeIMAP:
    def __init__(self, *args, **kwargs):
        self.calls = []
        self.validity = b"7"

    def login(self, *args):
        return "OK", []

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        return "OK", [b"1"]

    def response(self, name):
        return name, [self.validity]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "SEARCH":
            return "OK", [b"1"]
        if args[1] == "(RFC822.SIZE)":
            return "OK", [f"1 (RFC822.SIZE {len(RAW)})".encode()]
        return "OK", [(b'1 (INTERNALDATE "25-Nov-2026 12:00:00 +0000" BODY[] {74}', RAW), b")"]

    def logout(self):
        self.calls.append(("logout",))


class CorpusTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "data"
        self.store = Store(self.root)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def test_export_resume_and_uidvalidity_readonly(self):
        connection = FakeIMAP()
        options = dict(store=self.store, host="example.invalid", username="test", password="secret",
                       account="test", folders=["INBOX"], factory=lambda *a, **k: connection,
                       progress=lambda message: None)
        self.assertEqual(export(**options)[0]["downloaded"], 1)
        self.assertEqual(export(**options)[0]["already_stored"], 1)
        connection.validity = b"8"
        self.assertEqual(export(**options)[0]["downloaded"], 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 2)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
        self.assertTrue(all(call[2] for call in connection.calls if call[0] == "select"))
        body_calls = [call for call in connection.calls if call[0] == "FETCH" and "BODY" in call[2]]
        self.assertTrue(all("BODY.PEEK[]" in call[2] for call in body_calls))
        self.assertFalse(any(call[0] in ("STORE", "EXPUNGE") for call in connection.calls))

    def test_byte_budget_prevents_body_download(self):
        connection = FakeIMAP()
        result = export(self.store, "example.invalid", "test", "secret", "test", ["INBOX"],
                        max_bytes=1, factory=lambda *a, **k: connection, progress=lambda _: None)
        self.assertTrue(result[0]["budget_reached"])
        self.assertFalse(any(call[0] == "FETCH" and "BODY" in call[2] for call in connection.calls))

    def test_account_alias_cannot_mix_mailboxes(self):
        self.store.register_account("a", "example.invalid", 993, "user-a")
        with self.assertRaises(ValueError):
            self.store.register_account("a", "example.invalid", 993, "user-b")

    def test_interrupted_export_keeps_completed_messages(self):
        connection = FakeIMAP()
        original_uid = connection.uid

        def interrupted(command, *args):
            if command == "SEARCH":
                return "OK", [b"1 2"]
            if args[0] == b"1":
                raise OSError("synthetic disconnect")
            return original_uid(command, *args)

        connection.uid = interrupted
        with self.assertRaises(OSError):
            export(self.store, "example.invalid", "test", "secret", "a", ["INBOX"],
                   factory=lambda *a, **k: connection, progress=lambda _: None, retries=0)
        self.assertTrue(self.store.contains("a", "INBOX", "7", 2))
        self.assertFalse(self.store.contains("a", "INBOX", "7", 1))
        self.assertIn(("logout",), connection.calls)

    def test_malformed_sender_is_preserved_and_flagged(self):
        raw = (b"From: =?utf-8?Q?private=0Aname?= <test@example.invalid>\r\n"
               b"Subject: private subject\r\n\r\nprivate body\r\n")
        with self.assertLogs("hamlet", level="WARNING") as logs:
            digest = self.store.add("a", "Junk", "7", 1207, "2026-01-01", raw)
        row = self.store.db.execute(
            "SELECT sender,metadata_warnings FROM messages WHERE sha256=?", (digest,)
        ).fetchone()
        self.assertIn("=?utf-8?", row["sender"])
        self.assertEqual(json.loads(row["metadata_warnings"]),
                         [{"header": "From", "error_type": "ValueError"}])
        self.assertEqual(self.store.message_path(digest).read_bytes(), raw)
        self.assertNotIn("private", " ".join(logs.output))
        self.assertIn("metadata_fallback", " ".join(logs.output))

    def test_network_retry_reconnects_readonly_and_keeps_budget(self):
        first, second = FakeIMAP(), FakeIMAP()
        original_uid = first.uid

        def disconnected(command, *args):
            if command == "FETCH" and "BODY.PEEK" in args[1]:
                raise imaplib.IMAP4.abort("sensitive server response")
            return original_uid(command, *args)

        first.uid = disconnected
        connections = iter((first, second))
        sleeps = []
        with self.assertLogs("hamlet", level="WARNING") as logs:
            result = export(
                self.store, "example.invalid", "test", "secret", "a", ["INBOX"],
                max_bytes=len(RAW), factory=lambda *a, **k: next(connections),
                progress=lambda _: None, sleep=sleeps.append,
            )
        self.assertEqual(result[0]["downloaded"], 1)
        self.assertEqual(sleeps, [1])
        self.assertTrue(any(call[0] == "select" and call[2] for call in second.calls))
        self.assertNotIn("sensitive server response", " ".join(logs.output))
        self.assertNotIn("secret", " ".join(logs.output))

    def test_reconnect_rejects_changed_uidvalidity(self):
        first, second = FakeIMAP(), FakeIMAP()
        second.validity = b"8"
        original_uid = first.uid

        def disconnected(command, *args):
            if command == "FETCH":
                raise imaplib.IMAP4.abort("disconnected")
            return original_uid(command, *args)

        first.uid = disconnected
        connections = iter((first, second))
        with self.assertLogs("hamlet", level="ERROR"):
            with self.assertRaisesRegex(ExportError, "uidvalidity_changed_during_retry"):
                export(self.store, "example.invalid", "test", "secret", "a", ["INBOX"],
                       factory=lambda *a, **k: next(connections),
                       progress=lambda _: None, sleep=lambda _: None)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 0)

    def test_network_retries_are_bounded(self):
        attempts, sleeps = [], []

        def unavailable(*args, **kwargs):
            attempts.append(1)
            raise OSError("private diagnostic")

        with self.assertLogs("hamlet", level="WARNING") as logs:
            with self.assertRaises(OSError):
                export(self.store, "example.invalid", "test", "secret", "a", ["INBOX"],
                       factory=unavailable, progress=lambda _: None, retries=2,
                       sleep=sleeps.append)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(sleeps, [1, 2])
        self.assertNotIn("private diagnostic", " ".join(logs.output))
        self.assertIn("imap_retries_exhausted", " ".join(logs.output))

    def test_authentication_failures_are_not_retried(self):
        connection = FakeIMAP()
        connection.login = lambda *a: (_ for _ in ()).throw(
            imaplib.IMAP4.error("private authentication response"))
        with self.assertLogs("hamlet", level="ERROR") as logs:
            with self.assertRaises(imaplib.IMAP4.error):
                export(self.store, "example.invalid", "test", "secret", "a", ["INBOX"],
                       factory=lambda *a, **k: connection, sleep=lambda _: self.fail("retried"))
        self.assertNotIn("private authentication response", " ".join(logs.output))

    def test_certificate_failures_are_not_retried(self):
        def invalid_certificate(*args, **kwargs):
            raise ssl.SSLCertVerificationError("private certificate response")

        with self.assertLogs("hamlet", level="ERROR") as logs:
            with self.assertRaises(ssl.SSLCertVerificationError):
                export(self.store, "example.invalid", "test", "secret", "a", ["INBOX"],
                       factory=invalid_certificate, sleep=lambda _: self.fail("retried"))
        self.assertNotIn("private certificate response", " ".join(logs.output))

    def test_cli_unexpected_error_is_sanitized(self):
        with patch("hamlet.cli.run", side_effect=TypeError("private server payload")):
            with self.assertLogs("hamlet", level="ERROR") as logs:
                self.assertEqual(main(["--data-root", str(self.root), "corpus", "inventory"]), 1)
        self.assertIn("TypeError", " ".join(logs.output))
        self.assertNotIn("private server payload", " ".join(logs.output))

    def test_version_one_database_migrates_without_losing_labels(self):
        digest = self.store.add("a", "INBOX", "7", 1, "2026-01-01", RAW)
        self.store.review("a", digest, "ham")
        self.store.db.execute("ALTER TABLE messages DROP COLUMN metadata_warnings")
        self.store.db.execute("ALTER TABLE entries DROP COLUMN raw_internal_date")
        self.store.db.execute("ALTER TABLE entries DROP COLUMN date_warning")
        self.store.db.execute("DROP TABLE group_members")
        self.store.db.execute("DROP TABLE corpus_groups")
        self.store.db.execute("DROP TABLE grouping_runs")
        self.store.db.execute("PRAGMA user_version=1")
        self.store.close()
        self.store = Store(self.root)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        self.assertEqual(self.store.db.execute("SELECT label FROM reviews").fetchone()[0], "ham")
        self.assertEqual(self.store.db.execute(
            "SELECT metadata_warnings FROM messages").fetchone()[0], "[]")

    def test_bad_or_missing_internal_date_does_not_drop_message(self):
        for folder, date_field in (("bad-date", b' INTERNALDATE "not-a-date"'),
                                   ("missing-date", b"")):
            connection = FakeIMAP()
            original_uid = connection.uid

            def malformed_date(command, *args):
                if command == "FETCH" and "BODY.PEEK" in args[1]:
                    return "OK", [(b"1 (" + date_field + b" BODY[])", RAW), b")"]
                return original_uid(command, *args)

            connection.uid = malformed_date
            with self.assertLogs("hamlet", level="WARNING") as logs:
                result = export(
                    self.store, "example.invalid", "test", "secret", "a", [folder],
                    factory=lambda *a, **k: connection, progress=lambda _: None,
                )
            self.assertEqual(result[0]["downloaded"], 1)
            self.assertIn("date_fallback", " ".join(logs.output))
        rows = self.store.db.execute(
            "SELECT internal_date,raw_internal_date,date_warning FROM entries ORDER BY folder"
        ).fetchall()
        self.assertEqual(tuple(rows[0]), ("", "not-a-date", "internal_date_invalid"))
        self.assertEqual(tuple(rows[1]), ("", None, "internal_date_unavailable"))

    def test_metadata_parser_failure_does_not_drop_raw_message(self):
        with patch("email.parser.BytesParser.parsebytes",
                   side_effect=RuntimeError("private parser exception")):
            with self.assertLogs("hamlet", level="WARNING") as logs:
                digest = self.store.add("a", "Junk", "7", 1, "2026-01-01", RAW)
        self.assertEqual(self.store.message_path(digest).read_bytes(), RAW)
        self.assertTrue(self.store.contains("a", "Junk", "7", 1))
        self.assertNotIn("private parser exception", " ".join(logs.output))

    def test_bad_mime_encodings_and_missing_headers_are_archived(self):
        samples = [
            b"Subject: =?nonexistent-charset?b?/w==?=\r\n\r\nbody",
            b"From: \xff <bad@example.invalid>\r\n\r\nbody",
            b"Content-Type: multipart/mixed; boundary=\"unterminated\r\n\r\nbody",
            b"not a header at all\x00\xff",
        ]
        for uid, raw in enumerate(samples, 1):
            with self.subTest(uid=uid):
                digest = self.store.add("a", "Junk", "7", uid, "2026-01-01", raw)
                self.assertEqual(self.store.message_path(digest).read_bytes(), raw)
                self.assertTrue(self.store.contains("a", "Junk", "7", uid))

    def test_labels_are_append_only_and_per_account(self):
        digest = self.store.add("a", "INBOX", "1", 1, "2026-01-01T00:00:00+00:00", RAW)
        self.store.add("b", "spam", "1", 1, "2026-01-01T00:00:00+00:00", RAW)
        self.store.review("a", digest, "ham")
        self.store.review("a", digest, "unsure", "second review")
        self.store.review("b", digest, "spam")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0], 3)
        self.assertEqual(self.store.message_path(digest).stat().st_mode & 0o777, 0o600)
        snapshot = self.store.snapshot()
        self.assertEqual(len(json.loads(snapshot.read_text())["reviews"]), 3)
        self.assertEqual(snapshot, self.store.snapshot())

    def test_credentials_file_never_executes_shell(self):
        path = Path(self.temporary.name) / "credentials"
        path.write_text("export TEST_USER='user'\nexport TEST_PASS='literal value'\n")
        self.assertEqual(credentials("TEST_USER", "TEST_PASS", path), ("user", "literal value"))
        path.write_text("TEST_USER=$(touch /never-run)\nTEST_PASS=secret\n")
        with self.assertRaises(ValueError):
            credentials("TEST_USER", "TEST_PASS", path)
        path.write_text("BASE_USER=user\nBASE_PASS='literal $value'\n"
                        'TEST_USER="$BASE_USER"\nTEST_PASS="${BASE_PASS}"\n')
        self.assertEqual(credentials("TEST_USER", "TEST_PASS", path), ("user", "literal $value"))
        path.write_text("TEST_USER=$TEST_PASS\nTEST_PASS=$TEST_USER\n")
        with self.assertRaises(ValueError):
            credentials("TEST_USER", "TEST_PASS", path)

    def test_cli_review_inventory_and_integrity(self):
        digest = self.store.add("a", "INBOX", "1", 1, "2026-01-01T00:00:00+00:00", RAW)
        for command in (["inventory"], ["review", "--account", "a"], ["verify"]):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["--data-root", str(self.root), "corpus", *command]), 0)
                json.loads(output.getvalue())
        self.store.message_path(digest).write_bytes(b"corrupt")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--data-root", str(self.root), "corpus", "verify"]), 1)


if __name__ == "__main__":
    unittest.main()

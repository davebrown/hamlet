import base64
import hashlib
import quopri
import unittest
from unittest.mock import patch

from hamlet.normalization import normalize_message


ORIGINAL = (b"From: sender@example.invalid\r\nTo: receiver@example.invalid\r\n"
            b"Subject: Synthetic example\r\n\r\nSynthetic body.\r\n")
REPORT = (b"Spam detection software, running on the system example.invalid, has\n"
          b"identified this incoming email as possible spam. The original\n"
          b"message has been attached to this so you can view it.\n"
          b"Content analysis details: synthetic tests only.\n")


def wrapper(original=ORIGINAL, *, copies=1, kind=b"message/rfc822",
            encoding=b"7bit", body=REPORT, boundary=b"synthetic-boundary"):
    encoded = base64.b64encode(original) if encoding == b"base64" else original
    headers = (
        b"From: filter@example.invalid\nX-Spam-Flag: YES\n"
        b"X-Spam-Status: Yes, score=8.0 required=5.0 tests=SYNTHETIC\n"
        b"MIME-Version: 1.0\nContent-Type: multipart/mixed; boundary=\""
        + boundary + b"\"\n\n")
    result = headers + b"--" + boundary + b"\nContent-Type: text/plain\n\n" + body
    for _ in range(copies):
        result += (
            b"\n--" + boundary + b"\nContent-Type: " + kind
            + b"\nContent-Disposition: attachment; filename=\"original.eml\"\n"
            b"Content-Description: original message before SpamAssassin\n"
            b"Content-Transfer-Encoding: " + encoding + b"\n\n" + encoded)
    return result + b"\n--" + boundary + b"--\n"


class NormalizationTests(unittest.TestCase):
    def test_ordinary_forward_and_subject_are_not_evidence(self):
        raw = wrapper(body=b"Here is the forwarded message.\n").replace(
            b"Content-Description: original message before SpamAssassin\n", b"")
        raw = raw.replace(b"MIME-Version:", b"Subject: SpamAssassin report\nMIME-Version:")
        result = normalize_message(raw)
        self.assertEqual((result.status, result.content, result.steps),
                         ("unchanged", raw, ()))
        # Even canonical prose is insufficient without independent metadata.
        raw = wrapper().replace(b"X-Spam-Flag: YES\n", b"").replace(
            b"X-Spam-Status: Yes, score=8.0 required=5.0 tests=SYNTHETIC\n", b"").replace(
            b"Content-Description: original message before SpamAssassin\n", b"")
        self.assertEqual(normalize_message(raw).status, "unchanged")

    def test_incomplete_report_evidence_is_not_groupable_as_unchanged(self):
        for raw in (
                wrapper(body=b"Spam detection software, running on example.invalid.\n"),
                wrapper(body=b"Truncated report text.\n"),
                wrapper().replace(b"Content-Type: text/plain\n\n",
                                  b"Content-Type: text/plain\n"
                                  b"Content-Transfer-Encoding: base64\n\n", 1),
                b"X-Spam-Flag: YES\n\n" + REPORT,
                wrapper().split(b"\n--synthetic-boundary\nContent-Type: message")[0]):
            with self.subTest():
                result = normalize_message(raw)
                self.assertIn(result.status, ("ambiguous", "malformed"))
                self.assertEqual(result.content, raw)
                self.assertEqual(result.steps, ())
        nested = wrapper(wrapper(body=b"Truncated report.\n", boundary=b"inner"))
        result = normalize_message(nested)
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(len(result.steps), 1)

    def test_report_and_hash_provenance(self):
        raw = wrapper()
        snapshot = bytes(raw)
        result = normalize_message(raw)
        self.assertEqual(result.status, "unwrapped")
        self.assertIn(b"Synthetic body.", result.content)
        self.assertNotIn(b"Spam detection software", result.content)
        self.assertEqual(raw, snapshot)
        self.assertEqual(len(result.steps), 1)
        step = result.steps[0]
        self.assertEqual(step["wrapper_kind"], "spamassassin-report")
        self.assertEqual(step["part_path"], [1])
        self.assertEqual(step["source_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(step["extracted_sha256"], hashlib.sha256(result.content).hexdigest())
        self.assertEqual(step["serialization"], "stdlib-email-reserialized")
        self.assertFalse(step["byte_exact"])
        self.assertEqual(normalize_message(raw), result)

    def test_nested_wrappers_and_last_safe_content(self):
        inner = wrapper(boundary=b"inner")
        result = normalize_message(wrapper(inner))
        self.assertEqual(result.status, "unwrapped")
        self.assertEqual(len(result.steps), 2)
        self.assertEqual(result.steps[0]["extracted_sha256"],
                         result.steps[1]["source_sha256"])
        ambiguous = wrapper(copies=2, boundary=b"inner")
        result = normalize_message(wrapper(ambiguous))
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(len(result.steps), 1)
        self.assertIn(b"Spam detection software", result.content)

    def test_ambiguous_or_missing_original(self):
        for copies, warning in ((2, "multiple_originals"), (0, "missing_original")):
            with self.subTest(copies=copies):
                raw = wrapper(copies=copies)
                result = normalize_message(raw)
                self.assertEqual(result.status, "ambiguous")
                self.assertEqual(result.content, raw)
                self.assertEqual(result.steps, ())
                self.assertIn(warning, result.warnings)

    def test_transfer_encodings_and_safe_variants(self):
        for kind in (b"message/rfc822", b"application/octet-stream", b"text/plain"):
            for encoding in (b"7bit", b"base64", b"quoted-printable"):
                with self.subTest(kind=kind, encoding=encoding):
                    raw = wrapper(kind=kind, encoding=encoding)
                    result = normalize_message(raw)
                    self.assertEqual(result.status, "unwrapped", result.warnings)
                    self.assertIn(b"Synthetic body.", result.content)
                    if kind != b"message/rfc822":
                        self.assertTrue(result.steps[0]["byte_exact"])
        original = ORIGINAL.replace(b"Synthetic body.", b"An encoded = and caf\xe9.")
        for kind in (b"message/rfc822", b"text/plain"):
            raw = wrapper(original, kind=kind, encoding=b"quoted-printable")
            raw = raw.replace(original, quopri.encodestring(original, quotetabs=True))
            result = normalize_message(raw)
            self.assertEqual(result.status, "unwrapped")
            self.assertIn(b"An encoded = and caf\xe9.", result.content)
        raw = wrapper(kind=b"text/plain").replace(
            b'Content-Disposition: attachment; filename="original.eml"\n', b"")
        self.assertEqual(normalize_message(raw).status, "unwrapped")

    def test_corrupt_encoded_original_is_not_extracted(self):
        for kind in (b"message/rfc822", b"application/octet-stream"):
            raw = wrapper(kind=kind, encoding=b"base64")
            raw = raw.replace(base64.b64encode(ORIGINAL), b"%%%bad base64%%%")
            result = normalize_message(raw)
            self.assertEqual(result.status, "malformed")
            self.assertEqual(result.content, raw)
            self.assertEqual(result.steps, ())
            self.assertIn("invalid_base64", result.warnings)
        for original in (b"no headers\n", b"", b"From: broken\n\nbad=XY"):
            result = normalize_message(wrapper(original, kind=b"text/plain",
                                               encoding=b"quoted-printable"))
            self.assertEqual(result.status, "malformed")
            self.assertEqual(result.steps, ())

    def test_malformed_mime_falls_back_without_contents_in_warnings(self):
        for raw in (wrapper()[:-25], b"Content-Type: multipart/mixed\n\nbroken",
                    b"broken header\nprivate content"):
            result = normalize_message(raw)
            self.assertEqual(result.status, "malformed")
            self.assertEqual(result.content, raw)
            self.assertEqual(result.steps, ())
            self.assertTrue(all("private" not in warning for warning in result.warnings))
        raw = wrapper(b"From: sender@example.invalid\n"
                      b"Content-Type: multipart/mixed\n\nbroken inner MIME\n")
        result = normalize_message(raw)
        self.assertEqual((result.status, result.content, result.steps),
                         ("malformed", raw, ()))
        self.assertIn("original_mime_defect", result.warnings)
        nested_forward = (
            b"From: sender@example.invalid\nContent-Type: multipart/mixed; boundary=forward\n"
            b"\n--forward\nContent-Type: message/rfc822\n\n"
            b"From: nested@example.invalid\nContent-Type: multipart/mixed\n\nbroken\n"
            b"--forward--\n")
        raw = wrapper(nested_forward)
        self.assertEqual(normalize_message(raw).status, "malformed")
        self.assertEqual(normalize_message(raw), normalize_message(raw))

    def test_size_and_nesting_limits(self):
        raw = wrapper()
        with patch("hamlet.normalization.MAX_MESSAGE_BYTES", len(raw) - 1):
            result = normalize_message(raw)
        self.assertEqual((result.status, result.content, result.steps),
                         ("limited", raw, ()))
        nested = wrapper(wrapper(boundary=b"inner"))
        with patch("hamlet.normalization.MAX_WRAPPER_LAYERS", 1):
            result = normalize_message(nested)
        self.assertEqual(result.status, "limited")
        self.assertEqual(len(result.steps), 1)
        self.assertIn("wrapper_depth_limit", result.warnings)
        with patch("hamlet.normalization.MAX_MIME_PARTS", 2):
            self.assertEqual(normalize_message(raw).status, "limited")
        deep = ORIGINAL
        for index in range(40):
            boundary = f"depth-{index}".encode()
            deep = (b"From: test@example.invalid\nContent-Type: multipart/mixed; boundary="
                    + boundary + b"\n\n--" + boundary + b"\n" + deep
                    + b"\n--" + boundary + b"--\n")
        result = normalize_message(deep)
        self.assertEqual((result.status, result.content), ("limited", deep))
        deep = ORIGINAL
        for index in range(10):
            deep = wrapper(deep, boundary=f"wrapper-{index}".encode())
        result = normalize_message(deep)
        self.assertEqual(result.status, "limited")
        self.assertEqual(len(result.steps), 8)

    def test_conflicting_mime_and_unsupported_transfer_encoding(self):
        raw = wrapper().replace(b"Content-Type: message/rfc822",
                                b"Content-Type: message/rfc822\nContent-Type: text/plain")
        result = normalize_message(raw)
        self.assertEqual((result.status, result.content, result.steps),
                         ("ambiguous", raw, ()))
        self.assertIn("duplicate_mime_headers", result.warnings)
        raw = wrapper(encoding=b"x-unknown")
        result = normalize_message(raw)
        self.assertEqual((result.status, result.content), ("malformed", raw))
        self.assertIn("unsupported_transfer_encoding", result.warnings)

    def test_parser_exception_boundaries(self):
        raw = wrapper()
        for error, status, warning in (
                (RecursionError(), "limited", "parser_recursion_limit"),
                (ValueError("private message content"), "malformed", "mime_parse_failure")):
            with patch("hamlet.normalization.BytesParser.parsebytes", side_effect=error):
                result = normalize_message(raw)
            self.assertEqual((result.status, result.content), (status, raw))
            self.assertEqual(result.warnings, (warning,))

    def test_headerless_original_and_broken_charset(self):
        raw = wrapper(b"not an email", kind=b"text/plain")
        self.assertEqual(normalize_message(raw).status, "malformed")
        raw = wrapper().replace(b"Content-Type: text/plain\n",
                                b"Content-Type: text/plain; charset=not-a-charset\n", 1)
        result = normalize_message(raw)
        self.assertEqual(result.status, "unwrapped")
        self.assertIn("report_charset_fallback", result.warnings)
        # Metadata on the attachment corroborates genuine reports lacking spam headers.
        raw = wrapper().replace(b"X-Spam-Flag: YES\n", b"").replace(
            b"X-Spam-Status: Yes, score=8.0 required=5.0 tests=SYNTHETIC\n", b"")
        self.assertEqual(normalize_message(raw).status, "unwrapped")

    def test_older_report_prose_and_alternative_container(self):
        body = (b"Spam detection software, running on example.invalid.\n"
                b"The original message has been attached to this so you can view it.\n"
                b"Content analysis details: synthetic report.\n")
        for root in (b"multipart/mixed", b"multipart/alternative"):
            raw = wrapper(body=body).replace(b"Content-Type: multipart/mixed",
                                             b"Content-Type: " + root, 1)
            self.assertEqual(normalize_message(raw).status, "unwrapped")

    def test_eight_bit_report_and_original_preserve_payload_bytes(self):
        body = REPORT + b"\nSynthetic punctuation: \xe2\x80\x99\n"
        original = ORIGINAL.replace(b"Synthetic body.", b"Eight bit caf\xe9.")
        for kind in (b"message/rfc822", b"text/plain"):
            raw = wrapper(original, body=body, kind=kind, encoding=b"8bit")
            raw = raw.replace(b"Content-Type: text/plain\n\n",
                              b"Content-Type: text/plain; charset=utf-8\n"
                              b"Content-Transfer-Encoding: 8bit\n\n", 1)
            result = normalize_message(raw)
            self.assertEqual(result.status, "unwrapped", result.warnings)
            self.assertIn(b"Eight bit caf\xe9.", result.content)
            if kind == b"text/plain":
                self.assertEqual(result.content, original)


if __name__ == "__main__":
    unittest.main()

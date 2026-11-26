"""Conservative, bounded SpamAssassin report extraction; never modifies sources.

Recognition requires the canonical report prose plus independent SpamAssassin
metadata, not a subject or an attached message alone. Hashes describe the actual
input/output of each layer, not a claim that MIME reserialization is lossless.
All warnings are fixed codes, never message text. Limits apply per layer.
Inputs are limited to 5 MB, 512 outer MIME parts, 32 outer MIME levels, and
eight wrapper extractions. Recognition is structural, not sender authentication.
"""

import base64
import binascii
from dataclasses import dataclass
from email import policy
from email.errors import MessageError
from email.generator import BytesGenerator
from email.parser import BytesParser
import hashlib
import io
import quopri
import re


NORMALIZATION_VERSION = "spamassassin-report-v1"
MAX_MESSAGE_BYTES = 5_000_000
MAX_WRAPPER_LAYERS = 8
MAX_MIME_DEPTH = 32
MAX_MIME_PARTS = 512
MAX_REPORT_TEXT_BYTES = 100_000

_POLICY = policy.compat32.clone(linesep="\n", max_line_length=0)
_PARSE_ERRORS = (MessageError, ValueError, TypeError, UnicodeError, IndexError)


@dataclass(frozen=True)
class NormalizedMessage:
    content: bytes
    status: str
    steps: tuple[dict, ...]
    warnings: tuple[str, ...]


class _Unsafe(Exception):
    def __init__(self, status, warning):
        self.status = status
        self.warning = warning


def _decode(part, payload):
    encoding = str(part.get("Content-Transfer-Encoding", "7bit")).strip().lower()
    if encoding == "base64":
        try:
            return base64.b64decode(re.sub(rb"[ \t\r\n]", b"", payload), validate=True)
        except (binascii.Error, ValueError):
            raise _Unsafe("malformed", "invalid_base64") from None
    if encoding == "quoted-printable":
        if re.search(rb"=(?![0-9a-fA-F]{2}|\r?\n)", payload):
            raise _Unsafe("malformed", "invalid_quoted_printable")
        return quopri.decodestring(payload)
    if encoding not in ("7bit", "8bit", "binary", ""):
        raise _Unsafe("malformed", "unsupported_transfer_encoding")
    return payload


def _bytes_payload(part):
    encoding = str(part.get("Content-Transfer-Encoding", "7bit")).strip().lower()
    if encoding in ("7bit", "8bit", "binary", ""):
        # get_payload(False) can replace surrogateescaped wire bytes with decoded
        # Unicode. The identity-CTE decode path recovers the original bytes.
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            raise _Unsafe("malformed", "invalid_mime_payload")
        return payload
    payload = part.get_payload(decode=False)
    if not isinstance(payload, str):
        raise _Unsafe("malformed", "invalid_mime_payload")
    return payload.encode("ascii", "surrogateescape")


def _tree(message, *, include_attached=False):
    """Do not descend into attached messages; those are separate layers."""
    pending = [(message, ())]
    result = []
    while pending:
        part, path = pending.pop()
        if len(path) > MAX_MIME_DEPTH or len(result) >= MAX_MIME_PARTS:
            raise _Unsafe("limited", "mime_structure_limit")
        result.append((part, path))
        if part.get_content_type() == "message/rfc822" and not include_attached:
            continue
        payload = part.get_payload()
        if part.is_multipart():
            if not isinstance(payload, list):
                raise _Unsafe("malformed", "invalid_mime_payload")
            pending.extend((child, path + (index,))
                           for index, child in reversed(list(enumerate(payload))))
    return result


def _report(nodes, warnings):
    descriptions = False
    prose = False
    partial_prose = False
    root = nodes[0][0]
    header = (
        str(root.get("X-Spam-Flag", "")).strip().lower() == "yes"
        or re.match(r"yes\b", str(root.get("X-Spam-Status", "")).strip(), re.I)
        or "spamassassin" in str(root.get("X-Spam-Checker-Version", "")).lower()
    )
    for part, path in nodes:
        description = str(part.get("Content-Description", "")).lower()
        if "original message before spamassassin" in description:
            descriptions = True
        # Report prose must be in the wrapper's inline body, never its original.
        if (part.get_content_type() != "text/plain"
                or part.get_content_disposition() == "attachment"
                or part.get_filename()
                or "original message before spamassassin" in description):
            continue
        try:
            payload = _bytes_payload(part)
            if len(payload) > MAX_REPORT_TEXT_BYTES:
                warnings.add("report_text_limit")
                continue
            payload = _decode(part, payload)
            charset = part.get_content_charset() or "ascii"
            try:
                text = payload.decode(charset)
            except (LookupError, UnicodeError):
                warnings.add("report_charset_fallback")
                text = payload.decode("ascii", errors="replace")
            text = " ".join(text.lower().split())
            if ("spam detection software" in text
                    or "identified this incoming email as possible spam" in text
                    or ("spamassassin" in text and "content analysis details" in text)):
                partial_prose = True
            if ("spam detection software" in text
                    and (("identified this incoming email as possible spam" in text
                          and ("original message has been attached" in text
                               or "content analysis details" in text))
                         or ("original message has been attached" in text
                             and "content analysis details" in text))):
                prose = True
        except (_Unsafe, UnicodeError):
            warnings.add("report_text_decode_failure")
    return (bool(prose and (header or descriptions)),
            bool(descriptions or (partial_prose and header)))


def _candidates(nodes):
    result = []
    for part, path in nodes:
        if not path:
            continue
        kind = part.get_content_type()
        description = str(part.get("Content-Description", "")).lower()
        named_eml = (part.get_filename() or "").lower().endswith(".eml")
        original = "original message before spamassassin" in description
        if (kind == "message/rfc822"
                or (kind in ("application/octet-stream", "text/plain")
                    and (original or named_eml))):
            result.append((part, path))
    return result


def _extract(part):
    if part.get_content_type() == "message/rfc822":
        payload = part.get_payload()
        if not isinstance(payload, list) or len(payload) != 1:
            raise _Unsafe("malformed", "invalid_attached_message")
        # A generator can repair malformed MIME (including inventing boundaries).
        # Reject defects before serialization so repair cannot conceal uncertainty.
        encoding = str(part.get("Content-Transfer-Encoding", "7bit")).strip().lower()
        if encoding != "base64" and any(
                node.defects for node, _ in _tree(payload[0], include_attached=True)):
            raise _Unsafe("malformed", "original_mime_defect")
        buffer = io.BytesIO()
        BytesGenerator(buffer, policy=_POLICY, mangle_from_=False).flatten(part)
        serialized = buffer.getvalue()
        if b"\n\n" not in serialized:
            raise _Unsafe("malformed", "missing_attachment_body")
        content = _decode(part, serialized.split(b"\n\n", 1)[1])
        return content, "stdlib-email-reserialized", False
    return _decode(part, _bytes_payload(part)), "mime-payload-transfer-decoded", True


def normalize_message(raw: bytes) -> NormalizedMessage:
    """Extract only recognized, unique originals, retaining the last safe layer.

    ``unchanged`` means no wrapper recognized; ``unwrapped`` means all recognized
    layers succeeded.     ``ambiguous`` includes suspected reports and recognized reports with no
    original or multiple candidates. ``malformed`` and ``limited`` indicate unsafe input
    or a resource bound. Unsuccessful layers never change content or add a step.
    ``byte_exact`` refers to the decoded MIME payload, not the sender's wire mail.
    """
    if not isinstance(raw, bytes):
        raise TypeError("raw must be bytes")
    content = raw
    steps = []
    warnings = set()
    status = "unchanged"
    try:
        while True:
            if len(content) > MAX_MESSAGE_BYTES:
                raise _Unsafe("limited", "message_size_limit")
            message = BytesParser(policy=_POLICY).parsebytes(content)
            nodes = _tree(message)
            recognized, suspected = _report(nodes, warnings)
            if any(len(part.get_all(name, [])) > 1 for part, _ in nodes
                   for name in ("Content-Type", "Content-Transfer-Encoding",
                                "Content-Disposition")):
                raise _Unsafe("ambiguous" if recognized or suspected else "malformed",
                              "duplicate_mime_headers")
            if any(part.defects for part, _ in nodes):
                raise _Unsafe("malformed", "mime_defect")
            if not recognized:
                if suspected:
                    raise _Unsafe("ambiguous", "unconfirmed_report_wrapper")
                status = "unwrapped" if steps else "unchanged"
                break
            if len(steps) >= MAX_WRAPPER_LAYERS:
                raise _Unsafe("limited", "wrapper_depth_limit")
            if message.get_content_type() not in ("multipart/mixed", "multipart/alternative"):
                raise _Unsafe("malformed", "invalid_report_container")
            candidates = _candidates(nodes)
            if len(candidates) != 1:
                raise _Unsafe("ambiguous", "multiple_originals" if candidates
                              else "missing_original")
            part, path = candidates[0]
            extracted, serialization, byte_exact = _extract(part)
            if len(extracted) > MAX_MESSAGE_BYTES:
                raise _Unsafe("limited", "message_size_limit")
            # Reject empty/headerless or broken originals before recording success.
            original = BytesParser(policy=_POLICY).parsebytes(extracted)
            if not any(original.get(name) for name in
                       ("From", "To", "Date", "Subject", "Message-ID")):
                raise _Unsafe("malformed", "missing_original_headers")
            original_nodes = _tree(original)
            if any(node.defects for node, _ in original_nodes):
                raise _Unsafe("malformed", "original_mime_defect")
            steps.append({
                "wrapper_kind": "spamassassin-report",
                "part_path": list(path),
                "source_sha256": hashlib.sha256(content).hexdigest(),
                "extracted_sha256": hashlib.sha256(extracted).hexdigest(),
                "serialization": serialization,
                "byte_exact": byte_exact,
            })
            if not byte_exact:
                warnings.add("attached_message_reserialized")
            content = extracted
    except _Unsafe as error:
        status = error.status
        warnings.add(error.warning)
    except RecursionError:
        status = "limited"
        warnings.add("parser_recursion_limit")
    except _PARSE_ERRORS:
        status = "malformed"
        warnings.add("mime_parse_failure")
    return NormalizedMessage(content, status, tuple(steps), tuple(sorted(warnings)))

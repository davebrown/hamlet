import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from .diagnostics import event


SCHEMA_VERSION = 4
FOLDER_LABELS = {
    "ham-sample": ("ham", "user-folder"),
    "Junk": ("spam", "user-folder"),
    "INBOX": ("ham", "classifier"),
    "spam": ("spam", "classifier"),
}


def private_directory(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def extract_headers(raw):
    from email import policy
    from email.parser import BytesParser

    headers, warnings = [], []
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
        raw_headers = {}
        for name, value in message.raw_items():
            raw_headers.setdefault(name.lower(), value)
        warnings.extend({"header": None, "error_type": type(defect).__name__}
                        for defect in message.defects)
    except Exception as error:
        message, raw_headers = None, {}
        warnings.append({"header": None, "error_type": type(error).__name__})
    for name in ("Message-ID", "Subject", "From", "Date", "In-Reply-To",
                 "References", "X-Spam-Score"):
        try:
            header = message.get(name, "") if message is not None else ""
            value = str(header)
            warnings.extend({"header": name, "error_type": type(defect).__name__}
                            for defect in getattr(header, "defects", ()))
        except Exception as error:
            value = raw_headers.get(name.lower(), "")
            warnings.append({"header": name, "error_type": type(error).__name__})
        headers.append(value.encode("utf-8", errors="replace").decode("utf-8"))
    return headers, warnings


def content_identity(raw):
    """Canonicalize text wire line endings only when MIME makes that safe.

    This is a conservative grouping identity, never source identity or a
    signature/authentication check. Binary/unencoded non-text parts and parser
    uncertainty keep the exact byte hash instead.
    """
    digest = hashlib.sha256(raw).hexdigest()
    if b"\r\n" not in raw:
        return digest
    from email import policy
    from email.errors import MessageError
    from email.parser import BytesParser

    from .normalization import MAX_MESSAGE_BYTES, MAX_MIME_DEPTH, MAX_MIME_PARTS

    if len(raw) > MAX_MESSAGE_BYTES:
        return digest
    try:
        root = BytesParser(policy=policy.compat32).parsebytes(raw)
        pending = [(root, 0)]
        count = 0
        while pending:
            part, depth = pending.pop()
            count += 1
            if (count > MAX_MIME_PARTS or depth > MAX_MIME_DEPTH or part.defects
                    or part.get_content_type() in ("multipart/signed", "multipart/encrypted")
                    or len(part.get_all("Content-Type", [])) > 1
                    or len(part.get_all("Content-Transfer-Encoding", [])) > 1):
                return digest
            encoding = str(part.get("Content-Transfer-Encoding", "7bit")).strip().lower()
            if encoding not in ("7bit", "8bit", "base64", "quoted-printable", ""):
                return digest
            if part.is_multipart():
                pending.extend((child, depth + 1) for child in part.get_payload())
            elif encoding in ("base64", "quoted-printable"):
                if (encoding == "quoted-printable"
                        and part.get_content_maintype() != "text"):
                    return digest
                part.get_payload(decode=True)
                if part.defects:
                    return digest
            elif part.get_content_maintype() != "text":
                return digest
            else:
                payload = part.get_payload(decode=True)
                if not isinstance(payload, bytes) or re.search(
                        rb"[\x00-\x08\x0b\x0c\x0e-\x1f]", payload):
                    return digest
        return hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()
    except (MessageError, ValueError, TypeError, UnicodeError, IndexError, RecursionError):
        return digest


class Store:
    def __init__(self, root):
        self.root = private_directory(Path(root).expanduser().resolve())
        self.messages = private_directory(self.root / "messages")
        for name in ("snapshots", "runs", "spamassassin", "derived"):
            private_directory(self.root / name)
        path = self.root / "corpus.sqlite"
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        path.chmod(0o600)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3, SCHEMA_VERSION):
            self.db.close()
            raise ValueError(f"Unsupported corpus schema version: {version}")
        if 0 < version < 4:
            backup = self.root / "snapshots" / f"pre-normalization-schema{version}.sqlite"
            if not backup.exists():
                fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
                destination = sqlite3.connect(backup)
                try:
                    self.db.backup(destination)
                finally:
                    destination.close()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                account TEXT PRIMARY KEY, host TEXT NOT NULL,
                port INTEGER NOT NULL, username TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                sha256 TEXT PRIMARY KEY, size INTEGER NOT NULL,
                message_id TEXT, subject TEXT, sender TEXT, header_date TEXT,
                in_reply_to TEXT, references_header TEXT,
                spam_score TEXT
            );
            CREATE TABLE IF NOT EXISTS entries (
                account TEXT NOT NULL, folder TEXT NOT NULL,
                uidvalidity TEXT NOT NULL, uid INTEGER NOT NULL,
                sha256 TEXT NOT NULL REFERENCES messages(sha256),
                internal_date TEXT NOT NULL,
                provisional_label TEXT, label_source TEXT,
                exported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(account, folder, uidvalidity, uid)
            );
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY,
                account TEXT NOT NULL,
                sha256 TEXT NOT NULL REFERENCES messages(sha256),
                label TEXT NOT NULL CHECK(label IN ('ham','spam','unsure')),
                note TEXT NOT NULL DEFAULT '',
                reviewed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS entries_hash ON entries(sha256);
            CREATE INDEX IF NOT EXISTS reviews_message ON reviews(account,sha256,id);
        """)
        if version < 2:
            with self.db:
                self.db.execute("BEGIN")
                self.db.execute(
                    "ALTER TABLE messages ADD COLUMN metadata_warnings TEXT NOT NULL DEFAULT '[]'"
                )
                self.db.execute("ALTER TABLE entries ADD COLUMN raw_internal_date TEXT")
                self.db.execute("ALTER TABLE entries ADD COLUMN date_warning TEXT")
                self.db.execute("PRAGMA user_version=2")
        if version < 3:
            self.db.executescript("""
                BEGIN;
                CREATE TABLE grouping_runs (
                    run_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL,
                    parameters TEXT NOT NULL, report_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE corpus_groups (
                    run_id TEXT NOT NULL REFERENCES grouping_runs(run_id),
                    group_id TEXT NOT NULL, account TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('ham_thread','spam_campaign')),
                    anchor_sha256 TEXT NOT NULL REFERENCES messages(sha256),
                    PRIMARY KEY(run_id,group_id)
                );
                CREATE TABLE group_members (
                    run_id TEXT NOT NULL, group_id TEXT NOT NULL,
                    sha256 TEXT NOT NULL REFERENCES messages(sha256),
                    evidence TEXT NOT NULL, similarity REAL,
                    PRIMARY KEY(run_id,group_id,sha256),
                    FOREIGN KEY(run_id,group_id) REFERENCES corpus_groups(run_id,group_id)
                );
                CREATE INDEX group_members_message ON group_members(sha256);
                PRAGMA user_version=3;
                COMMIT;
            """)
        if version < 4:
            self.db.executescript("""
                BEGIN;
                CREATE TABLE IF NOT EXISTS normalized_messages (
                    source_sha256 TEXT NOT NULL REFERENCES messages(sha256),
                    algorithm TEXT NOT NULL, effective_sha256 TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN
                        ('unchanged','unwrapped','ambiguous','malformed','limited')),
                    metadata TEXT NOT NULL, steps TEXT NOT NULL, warnings TEXT NOT NULL,
                    PRIMARY KEY(source_sha256,algorithm)
                );
                CREATE INDEX IF NOT EXISTS normalized_effective
                    ON normalized_messages(algorithm,effective_sha256);
                CREATE TABLE IF NOT EXISTS normalization_runs (
                    run_id TEXT PRIMARY KEY, algorithm TEXT NOT NULL,
                    snapshot TEXT NOT NULL, report_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                PRAGMA user_version=4;
                COMMIT;
            """)

    def close(self):
        self.db.close()

    def register_account(self, account, host, port, username):
        identity = (host.lower(), port, username)
        row = self.db.execute(
            "SELECT host,port,username FROM accounts WHERE account=?", (account,)
        ).fetchone()
        if row is not None and tuple(row) != identity:
            raise ValueError("Account alias already belongs to a different IMAP identity")
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO accounts VALUES (?,?,?,?)", (account, *identity)
            )

    def contains(self, account, folder, validity, uid):
        return self.db.execute(
            "SELECT 1 FROM entries WHERE account=? AND folder=? AND uidvalidity=? AND uid=?",
            (account, folder, validity, uid),
        ).fetchone() is not None

    def message_path(self, digest):
        return self.messages / digest[:2] / f"{digest}.eml"

    def derived_path(self, digest):
        return self.root / "derived" / digest[:2] / f"{digest}.eml"

    def save_normalized(self, source, algorithm, result):
        digest = hashlib.sha256(result.content).hexdigest()
        if digest != source:
            path = self.derived_path(digest)
            private_directory(path.parent)
            if path.exists():
                with path.open("rb") as stream:
                    existing = hashlib.file_digest(stream, "sha256").hexdigest()
                if path.stat().st_size != len(result.content) or existing != digest:
                    raise ValueError("Derived integrity check failed")
            else:
                fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".normalize-")
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(result.content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
        headers, metadata_warnings = extract_headers(result.content)
        names = ("message_id", "subject", "sender", "header_date", "in_reply_to",
                 "references_header", "spam_score")
        metadata = dict(zip(names, headers))
        metadata["metadata_warnings"] = metadata_warnings
        metadata["content_identity"] = (
            content_identity(result.content)
            if result.status in ("unchanged", "unwrapped") else digest)
        with self.db:
            self.db.execute(
                "INSERT INTO normalized_messages VALUES (?,?,?,?,?,?,?,?)",
                (source, algorithm, digest, len(result.content), result.status,
                 json.dumps(metadata), json.dumps(result.steps), json.dumps(result.warnings)),
            )

    def effective_path(self, source, effective):
        return self.message_path(source) if source == effective else self.derived_path(effective)

    def add(self, account, folder, validity, uid, internal_date, raw, *,
            raw_internal_date=None, date_warning=None):
        digest = hashlib.sha256(raw).hexdigest()
        path = self.message_path(digest)
        private_directory(path.parent)
        if not path.exists():
            fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".download-")
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        headers, warnings = extract_headers(raw)
        label, source = FOLDER_LABELS.get(folder, (None, "unknown-folder"))
        with self.db:
            self.db.execute(
                """INSERT OR IGNORE INTO messages
                   (sha256,size,message_id,subject,sender,header_date,in_reply_to,
                    references_header,spam_score,metadata_warnings)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (digest, len(raw), *headers, json.dumps(warnings)),
            )
            self.db.execute(
                """INSERT OR IGNORE INTO entries
                   (account,folder,uidvalidity,uid,sha256,internal_date,
                    provisional_label,label_source,raw_internal_date,date_warning)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (account, folder, validity, uid, digest, internal_date, label, source,
                 raw_internal_date, date_warning),
            )
        if warnings:
            event("metadata_fallback", level=logging.WARNING, folder=folder, uid=uid,
                  uidvalidity=validity, warnings=warnings)
        if date_warning:
            event("date_fallback", level=logging.WARNING, folder=folder, uid=uid,
                  uidvalidity=validity, reason=date_warning)
        return digest

    def review(self, account, digest, label, note=""):
        if not self.db.execute(
            "SELECT 1 FROM entries WHERE account=? AND sha256=?", (account, digest)
        ).fetchone():
            raise ValueError("No matching message for this account")
        with self.db:
            self.db.execute(
                "INSERT INTO reviews(account,sha256,label,note) VALUES (?,?,?,?)",
                (account, digest, label, note),
            )

    def snapshot(self):
        entries = [dict(row) for row in self.db.execute(
            "SELECT * FROM entries ORDER BY account,folder,uidvalidity,uid"
        )]
        reviews = [dict(row) for row in self.db.execute("SELECT * FROM reviews ORDER BY id")]
        accounts = [dict(row) for row in self.db.execute("SELECT * FROM accounts ORDER BY account")]
        content = json.dumps(
            {"schema_version": SCHEMA_VERSION, "accounts": accounts,
             "entries": entries, "reviews": reviews},
            sort_keys=True, indent=2,
        ).encode()
        digest = hashlib.sha256(content).hexdigest()
        path = self.root / "snapshots" / f"{digest}.json"
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
        return path

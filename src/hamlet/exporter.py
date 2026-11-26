import imaplib
import logging
from pathlib import Path
import re
import shlex
import ssl
import time
from datetime import datetime

from .diagnostics import event, failure_fields
from .config import read_environment


class ExportError(RuntimeError):
    """An exporter failure with a content-free diagnostic code."""



def credentials(username_var, password_var, credentials_file=None, *, environment=None):
    environment = read_environment() if environment is None else environment
    assignments = {}
    if credentials_file:
        # Parse only literal assignments. Never source executable shell configuration.
        for line in Path(credentials_file).expanduser().read_text().splitlines():
            match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
            if match:
                assignments[match[1]] = match[2]

    def resolve(name, visited):
        if name in environment:
            return environment[name]
        if name in visited:
            raise ValueError("Circular credential variable reference")
        if name not in assignments:
            return None
        literal = assignments[name].strip()
        reference = re.fullmatch(
            r'"?\$(?:([A-Za-z_][A-Za-z0-9_]*)|\{([A-Za-z_][A-Za-z0-9_]*)\})"?', literal
        )
        if reference:
            return resolve(reference[1] or reference[2], visited | {name})
        single_quoted = re.fullmatch(r"'[^']*'\s*(?:#.*)?", literal)
        if not single_quoted and ("$" in literal or "`" in literal):
            raise ValueError("Credential file contains unsupported shell expansion")
        words = shlex.split(literal, comments=True)
        if len(words) != 1:
            raise ValueError("Credential assignment must contain one quoted or unquoted value")
        return words[0]

    username = resolve(username_var, set())
    password = resolve(password_var, set())
    if not username or not password:
        raise ValueError("Missing IMAP credentials; supply the configured environment variables")
    return username, password


def require_ok(result):
    status, data = result
    if status != "OK":
        raise ExportError("imap_operation_rejected")
    return data


class Session:
    def __init__(self, host, port, username, password, factory, retries, sleep):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.factory = factory
        self.retries = retries
        self.sleep = sleep
        self.connection = None
        self.folder = None
        self.validity = None

    def close(self):
        if self.connection is not None:
            connection, self.connection = self.connection, None
            try:
                connection.logout()
            except (OSError, imaplib.IMAP4.error):
                pass

    def _select(self):
        require_ok(self.connection.select(f'"{self.folder}"', readonly=True))
        _, data = self.connection.response("UIDVALIDITY")
        if not data or not data[0] or not data[0].isdigit():
            raise ExportError("uidvalidity_unavailable")
        validity = data[0].decode("ascii")
        if self.validity is not None and validity != self.validity:
            raise ExportError("uidvalidity_changed_during_retry")
        self.validity = validity

    def _connect(self):
        self.connection = self.factory(
            self.host, self.port, ssl_context=ssl.create_default_context(), timeout=60
        )
        require_ok(self.connection.login(self.username, self.password))
        if self.folder is not None:
            self._select()

    def perform(self, operation, function, uid=None):
        for attempt in range(self.retries + 1):
            try:
                if self.connection is None:
                    self._connect()
                return function()
            except ssl.SSLCertVerificationError:
                self.close()
                raise
            except (OSError, imaplib.IMAP4.abort) as error:
                self.close()
                if attempt == self.retries:
                    event("imap_retries_exhausted", level=logging.ERROR, operation=operation,
                          folder=self.folder, uid=uid, **failure_fields(error))
                    raise
                delay = min(2 ** attempt, 30)
                event("imap_retry", level=logging.WARNING, operation=operation,
                      folder=self.folder, uid=uid, retry=attempt + 1,
                      max_retries=self.retries, delay_seconds=delay,
                      error_type=type(error).__name__)
                self.sleep(delay)

    def connect(self):
        self.perform("connect", lambda: None)

    def select(self, folder):
        self.folder, self.validity = folder, None
        self.perform("select", self._select)
        return self.validity

    def uid(self, command, *args):
        uid = int(args[0]) if command == "FETCH" else None
        return self.perform(
            command.lower(), lambda: require_ok(self.connection.uid(command, *args)), uid=uid
        )


def export(store, host, username, password, account, folders, port=993,
           limit=None, max_bytes=2_000_000_000, progress=print, factory=None,
           retries=3, sleep=time.sleep):
    if factory is None:
        factory = imaplib.IMAP4_SSL
    if retries < 0:
        raise ValueError("Retries must not be negative")
    from .normalized import algorithm_identity, normalize_source

    normalization_algorithm = algorithm_identity()
    connection = Session(host, port, username, password, factory, retries, sleep)
    total_bytes = 0
    summary = []
    folder = None
    uid = None
    stage = "connect"
    try:
        connection.connect()
        store.register_account(account, host, port, username)
        for folder in folders:
            stage, uid = "select", None
            if any(character in folder for character in ('"', "\\", "\r", "\n")):
                raise ValueError("Unsupported folder name")
            validity = connection.select(folder)
            stage = "search"
            uids = connection.uid("SEARCH", None, "ALL")[0].split()
            uids.sort(key=int, reverse=True)
            downloaded = skipped = 0
            progress(f"{folder}: {len(uids)} server entries")
            event("folder_started", folder=folder, uidvalidity=validity, server_entries=len(uids))
            for uid_bytes in uids:
                uid = int(uid_bytes)
                if store.contains(account, folder, validity, uid):
                    stage = "normalize_stored_message"
                    source = store.db.execute(
                        "SELECT sha256 FROM entries "
                        "WHERE account=? AND folder=? AND uidvalidity=? AND uid=?",
                        (account, folder, validity, uid),
                    ).fetchone()[0]
                    normalize_source(store, source, algorithm=normalization_algorithm)
                    skipped += 1
                    continue
                if limit is not None and downloaded >= limit:
                    break
                stage = "fetch_size"
                metadata = connection.uid(
                    "FETCH", uid_bytes, "(RFC822.SIZE)"
                )
                size_match = re.search(rb"RFC822\.SIZE (\d+)", b" ".join(
                    item for item in metadata if isinstance(item, bytes)
                ))
                if not size_match:
                    raise ExportError("message_size_unavailable")
                if total_bytes + int(size_match[1]) > max_bytes:
                    progress("Run byte budget reached; rerun to resume")
                    return summary + [{"folder": folder, "downloaded": downloaded,
                                       "already_stored": skipped, "budget_reached": True}]
                stage = "fetch_body"
                fetched = connection.uid(
                    "FETCH", uid_bytes, "(INTERNALDATE BODY.PEEK[])"
                )
                parts = [item for item in fetched if isinstance(item, tuple)]
                if len(parts) != 1:
                    raise ExportError("message_body_unavailable")
                metadata, raw = parts[0]
                if len(raw) != int(size_match[1]):
                    raise ExportError("message_size_mismatch")
                stage = "parse_internal_date"
                date_match = re.search(rb'INTERNALDATE "([^"]*)"', metadata)
                raw_date = date_match[1].decode("ascii", errors="replace") if date_match else None
                date, date_warning = "", None
                if raw_date is None:
                    date_warning = "internal_date_unavailable"
                else:
                    try:
                        date = datetime.strptime(raw_date, "%d-%b-%Y %H:%M:%S %z").isoformat()
                    except ValueError:
                        date_warning = "internal_date_invalid"
                stage = "store_message"
                source = store.add(account, folder, validity, uid, date, raw,
                                   raw_internal_date=raw_date, date_warning=date_warning)
                stage = "normalize_message"
                normalize_source(store, source, raw=raw, algorithm=normalization_algorithm)
                total_bytes += len(raw)
                downloaded += 1
                if downloaded % 25 == 0:
                    progress(f"{folder}: downloaded {downloaded}; {total_bytes} bytes this run")
            summary.append({"folder": folder, "downloaded": downloaded, "already_stored": skipped})
            event("folder_finished", **summary[-1], downloaded_bytes_this_run=total_bytes)
        return summary
    except Exception as error:
        event("export_failed", level=logging.ERROR, stage=stage, folder=folder, uid=uid,
              downloaded_bytes_this_run=total_bytes,
              reason=str(error) if isinstance(error, ExportError) else None,
              **failure_fields(error))
        raise
    finally:
        connection.close()

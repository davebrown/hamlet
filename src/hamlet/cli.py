import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import sys

from .exporter import credentials, export
from .config import read_environment
from .diagnostics import event, failure_fields
from .store import FOLDER_LABELS, SCHEMA_VERSION, Store


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Must be greater than zero")
    return number


def nonnegative(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("Must not be negative")
    return number


def parser(environment=None):
    environment = os.environ if environment is None else environment
    root = argparse.ArgumentParser(prog="hamlet")
    root.add_argument("--data-root", default=environment.get("HAMLET_DATA_ROOT", "~/hamlet-data"))
    corpus = root.add_subparsers(dest="area", required=True).add_parser("corpus")
    commands = corpus.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    remote = commands.add_parser("export")
    remote.add_argument("--host", default=environment.get("HAMLET_IMAP_HOST"))
    remote.add_argument("--port", type=positive, default=993)
    remote.add_argument("--account", required=True, help="Stable local account alias; not a password")
    remote.add_argument("--folder", action="append", dest="folders")
    remote.add_argument("--username-var", default="HAMLET_IMAP_USERNAME")
    remote.add_argument("--password-var", default="HAMLET_IMAP_PASSWORD")
    remote.add_argument("--credentials-file", type=Path)
    remote.add_argument("--limit", type=positive, help="Maximum new downloads per folder, newest UID first")
    remote.add_argument("--max-bytes", type=positive, default=2_000_000_000)
    remote.add_argument("--retries", type=nonnegative, default=3,
                        help="Reconnect retries per network operation (default: 3)")
    commands.add_parser("inventory")
    commands.add_parser("verify")
    commands.add_parser("snapshot")
    commands.add_parser("normalize")
    commands.add_parser("normalization-report")
    grouping = commands.add_parser("group")
    grouping.add_argument("--spam-threshold", type=float, default=0.85)
    split = commands.add_parser("split")
    split.add_argument("--test-fraction", type=float, default=0.2)
    report = commands.add_parser("group-report")
    report.add_argument("--run-id")
    show = commands.add_parser("group-show")
    show.add_argument("group_id")
    show.add_argument("--run-id")
    review = commands.add_parser("review")
    review.add_argument("--account", required=True)
    review.add_argument("--folder")
    review.add_argument("--limit", type=positive, default=100)
    review.add_argument("--seed", default="hamlet-pilot-1")
    label = commands.add_parser("label")
    label.add_argument("--account", required=True)
    label.add_argument("--sha256", required=True)
    label.add_argument("--label", choices=("ham", "spam", "unsure"), required=True)
    label.add_argument("--note", default="")
    return root


def run(args, store):
    if args.command == "split":
        from .splitting import build_split
        return build_split(store, test_fraction=args.test_fraction)
    if args.command == "normalize":
        from .normalized import normalize_corpus
        return normalize_corpus(store)
    if args.command == "normalization-report":
        row = store.db.execute(
            "SELECT report_json FROM normalization_runs ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise ValueError("No normalization run")
        return json.loads(row[0])
    if args.command == "group":
        from .grouping import group_corpus
        return group_corpus(store, args.spam_threshold)
    if args.command in ("group-report", "group-show"):
        row = store.db.execute(
            "SELECT run_id,report_json FROM grouping_runs "
            + ("WHERE run_id=?" if args.run_id else "ORDER BY rowid DESC LIMIT 1"),
            (args.run_id,) if args.run_id else (),
        ).fetchone()
        if row is None:
            raise ValueError("No matching grouping run")
        if args.command == "group-report":
            return json.loads(row["report_json"])
        group = store.db.execute("""
            SELECT account,kind,anchor_sha256 FROM corpus_groups WHERE run_id=? AND group_id=?
        """, (row["run_id"], args.group_id)).fetchone()
        if group is None:
            raise ValueError("No matching group in this run")
        members = [dict(member) for member in store.db.execute("""
            SELECT sha256,evidence,similarity FROM group_members
            WHERE run_id=? AND group_id=? ORDER BY sha256
        """, (row["run_id"], args.group_id))]
        if not members:
            raise ValueError("No matching group in this run")
        for member in members:
            member["path"] = str(store.message_path(member["sha256"]))
            algorithm = json.loads(row["report_json"])["parameters"].get("normalization_algorithm")
            view = store.db.execute(
                "SELECT effective_sha256,status,steps,warnings FROM normalized_messages "
                "WHERE source_sha256=? AND algorithm=?", (member["sha256"], algorithm),
            ).fetchone()
            if view:
                member["effective_path"] = str(store.effective_path(
                    member["sha256"], view["effective_sha256"]))
                member["normalization"] = {
                    **dict(view), "steps": json.loads(view["steps"]),
                    "warnings": json.loads(view["warnings"]),
                }
        return {"run_id": row["run_id"], "group_id": args.group_id,
                **dict(group), "members": members}
    if args.command == "init":
        return {"data_root": str(store.root), "schema_version": SCHEMA_VERSION}
    if args.command == "export":
        if not args.host:
            raise ValueError("--host or HAMLET_IMAP_HOST is required")
        user, password = credentials(
            args.username_var, args.password_var, args.credentials_file,
            environment=getattr(args, "environment", None),
        )
        return export(store, args.host, user, password, args.account,
                      args.folders or list(FOLDER_LABELS), port=args.port,
                      limit=args.limit, max_bytes=args.max_bytes,
                      retries=args.retries,
                      progress=lambda text: event("progress", message=text))
    if args.command == "inventory":
        return {
            "folders": [dict(row) for row in store.db.execute("""
                SELECT account,folder,COUNT(*) AS entries,COUNT(DISTINCT sha256) AS unique_messages,
                       MIN(NULLIF(internal_date,'')) AS earliest,
                       MAX(NULLIF(internal_date,'')) AS latest,
                       SUM(date_warning IS NOT NULL) AS date_warnings
                FROM entries GROUP BY account,folder ORDER BY account,folder
            """)],
            "months": [dict(row) for row in store.db.execute("""
                SELECT account,folder,COALESCE(NULLIF(SUBSTR(internal_date,1,7),''),
                       'unknown') AS month,COUNT(*) AS entries
                FROM entries GROUP BY account,folder,month ORDER BY account,folder,month
            """)],
            "content": dict(store.db.execute(
                "SELECT COUNT(*) AS unique_messages,COALESCE(SUM(size),0) AS bytes FROM messages"
            ).fetchone()),
            "label_conflicts": [dict(row) for row in store.db.execute("""
                SELECT account,sha256 FROM entries WHERE provisional_label IS NOT NULL
                GROUP BY account,sha256 HAVING COUNT(DISTINCT provisional_label)>1
            """)],
            "review_events": store.db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0],
            "metadata_warning_messages": store.db.execute(
                "SELECT COUNT(*) FROM messages WHERE metadata_warnings != '[]'"
            ).fetchone()[0],
        }
    if args.command == "label":
        store.review(args.account, args.sha256, args.label, args.note)
        return {"recorded": True}
    if args.command == "review":
        query = """
            SELECT DISTINCT e.sha256 FROM entries e
            WHERE e.account=? AND NOT EXISTS (
                SELECT 1 FROM reviews r WHERE r.account=e.account AND r.sha256=e.sha256)
        """
        parameters = [args.account]
        if args.folder:
            query += " AND e.folder=?"
            parameters.append(args.folder)
        hashes = [row[0] for row in store.db.execute(query, parameters)]
        hashes.sort(key=lambda digest: hashlib.sha256(
            f"{args.seed}:{digest}".encode()).digest())
        return {"purpose": "development review, NOT a reserved final holdout",
                "seed": args.seed, "population": len(hashes), "messages": [
                    {"sha256": digest, "path": str(store.message_path(digest))}
                    for digest in hashes[:args.limit]
                ]}
    if args.command == "snapshot":
        return {"manifest": str(store.snapshot())}
    if args.command == "verify":
        failures = []
        checked = 0
        for row in store.db.execute("SELECT sha256,size FROM messages"):
            checked += 1
            path = store.message_path(row["sha256"])
            if not path.exists():
                failures.append({"sha256": row["sha256"], "error": "missing"})
            else:
                with path.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                if path.stat().st_size != row["size"] or digest != row["sha256"]:
                    failures.append({"sha256": row["sha256"], "error": "content mismatch"})
        derived_checked = 0
        for row in store.db.execute("""
            SELECT DISTINCT effective_sha256,size FROM normalized_messages
            WHERE effective_sha256 != source_sha256
        """):
            derived_checked += 1
            path = store.derived_path(row["effective_sha256"])
            if not path.exists():
                failures.append({"sha256": row["effective_sha256"], "error": "derived missing"})
            else:
                with path.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                if path.stat().st_size != row["size"] or digest != row["effective_sha256"]:
                    failures.append({"sha256": row["effective_sha256"],
                                     "error": "derived content mismatch"})
        normalized_checked = 0
        for row in store.db.execute("""
            SELECT n.*,m.size AS source_size FROM normalized_messages n
            JOIN messages m ON m.sha256=n.source_sha256
        """):
            normalized_checked += 1
            valid = True
            try:
                steps = json.loads(row["steps"])
                if not isinstance(steps, list):
                    raise ValueError("Invalid provenance")
                previous = row["source_sha256"]
                for step in steps:
                    path = step["part_path"]
                    if (step["wrapper_kind"] != "spamassassin-report"
                            or step["source_sha256"] != previous
                            or not isinstance(path, list) or not path
                            or any(type(index) is not int or index < 0 for index in path)
                            or not isinstance(step["serialization"], str)
                            or not step["serialization"]
                            or type(step["byte_exact"]) is not bool):
                        valid = False
                    previous = step["extracted_sha256"]
                valid = valid and previous == row["effective_sha256"]
                if row["status"] == "unchanged" and steps:
                    valid = False
                if row["status"] == "unwrapped" and not steps:
                    valid = False
                if (row["effective_sha256"] == row["source_sha256"]
                        and row["size"] != row["source_size"]):
                    valid = False
            except (ValueError, KeyError, TypeError):
                valid = False
            if not valid:
                failures.append({"sha256": row["source_sha256"],
                                 "error": "normalization provenance mismatch"})
        return {"checked": checked, "derived_checked": derived_checked,
                "normalized_checked": normalized_checked, "failures": failures}
    raise ValueError("Unknown command")


def main(argv=None):
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = None
    store = None
    try:
        environment = read_environment()
        args = parser(environment).parse_args(argv)
        args.environment = environment
        store = Store(args.data_root)
        result = run(args, store)
        print(json.dumps(result, indent=2))
        return 1 if args.command == "verify" and result["failures"] else 0
    except Exception as error:
        event("command_failed", level=logging.ERROR, command=getattr(args, "command", "configure"),
              **failure_fields(error))
        return 1
    finally:
        if store is not None:
            store.close()

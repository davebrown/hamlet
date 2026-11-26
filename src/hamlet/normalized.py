"""Versioned local derived views; source mail and labels are never modified."""

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import platform
import tempfile

from .diagnostics import event


def algorithm_identity():
    from . import normalization
    return ":".join((
        normalization.NORMALIZATION_VERSION,
        hashlib.sha256(Path(normalization.__file__).read_bytes()).hexdigest(),
        hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        hashlib.sha256(Path(__file__).with_name("store.py").read_bytes()).hexdigest(),
        platform.python_version(),
    ))


def normalize_source(store, source, *, raw=None, algorithm=None):
    """Ensure one source has a current, integrity-checked derived view."""
    from .normalization import normalize_message

    algorithm = algorithm or algorithm_identity()
    row = store.db.execute("SELECT size FROM messages WHERE sha256=?", (source,)).fetchone()
    if row is None:
        raise ValueError("Unknown normalization source")
    if raw is None:
        raw = store.message_path(source).read_bytes()
    if len(raw) != row["size"] or hashlib.sha256(raw).hexdigest() != source:
        raise ValueError("Source integrity check failed")
    result = store.db.execute(
        "SELECT * FROM normalized_messages WHERE source_sha256=? AND algorithm=?",
        (source, algorithm),
    ).fetchone()
    if result is None:
        store.save_normalized(source, algorithm, normalize_message(raw))
        return store.db.execute(
            "SELECT * FROM normalized_messages WHERE source_sha256=? AND algorithm=?",
            (source, algorithm),
        ).fetchone()
    if result["effective_sha256"] != source:
        path = store.derived_path(result["effective_sha256"])
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if path.stat().st_size != result["size"] or digest != result["effective_sha256"]:
            raise ValueError("Derived integrity check failed")
    elif result["size"] != len(raw):
        raise ValueError("Normalized integrity check failed")
    return result


def normalize_corpus(store):
    algorithm = algorithm_identity()
    snapshot = store.snapshot()
    run_id = hashlib.sha256((snapshot.stem + algorithm).encode()).hexdigest()
    statuses, warnings = Counter(), Counter()
    rows = store.db.execute("SELECT sha256,size FROM messages ORDER BY sha256").fetchall()
    for index, row in enumerate(rows):
        source = row["sha256"]
        result = normalize_source(store, source, algorithm=algorithm)
        statuses[result["status"]] += 1
        warnings.update(json.loads(result["warnings"]))
        if (index + 1) % 5000 == 0:
            event("normalization_progress", processed=index + 1, total=len(rows))
    effective = store.db.execute("""
        SELECT COUNT(DISTINCT effective_sha256) FROM normalized_messages
        WHERE algorithm=? AND status IN ('unchanged','unwrapped')
    """, (algorithm,)).fetchone()[0]
    report = {
        "run_id": run_id, "algorithm": algorithm, "input_snapshot": str(snapshot),
        "source_messages": len(rows), "statuses": dict(statuses), "warnings": dict(warnings),
        "safe_distinct_effective_messages": effective,
        "report_path": str(store.root / "runs" / f"normalization-{run_id}.json"),
        "limitations": [
            "Extraction is conservative, not authentication of SpamAssassin headers.",
            "Derived MIME serialization can differ from original wire bytes.",
            "Verdict headers are retained for provenance; not a classifier-ready input.",
        ],
    }
    content = json.dumps(report, indent=2, sort_keys=True)
    fd, temporary = tempfile.mkstemp(dir=store.root / "runs", prefix=".normalization-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content + "\n")
        os.replace(temporary, report["report_path"])
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    with store.db:
        store.db.execute(
            "INSERT OR REPLACE INTO normalization_runs "
            "(run_id,algorithm,snapshot,report_json) VALUES (?,?,?,?)",
            (run_id, algorithm, str(snapshot), content),
        )
    return report

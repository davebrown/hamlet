"""Private, provisional group-disjoint development/test manifests."""

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path

from . import grouping
from .normalized import algorithm_identity
from .store import private_directory


ALGORITHM_VERSION = "group-content-components-chronological-v1"


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _write(path, value):
    private_directory(path.parent)
    pending = path.with_suffix(path.suffix + ".pending")
    fd = os.open(pending, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
        path.chmod(0o600)
    finally:
        pending.unlink(missing_ok=True)


def _date(source):
    if source["date_warning"]:
        return None
    try:
        value = datetime.fromisoformat(source["internal_date"].replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def _source_distribution(records):
    counts = Counter(
        (record["account"], source["folder"], source["label_source"] or "unknown",
         source["provisional_label"] or "unlabeled")
        for record in records for source in record["sources"]
    )
    return [
        {"account": account, "folder": folder, "label_source": label_source,
         "provisional_label": label, "entries": count}
        for (account, folder, label_source, label), count in sorted(counts.items())
    ]


def _grouping_run(store, snapshot, algorithm):
    implementation = hashlib.sha256(Path(grouping.__file__).read_bytes()).hexdigest()
    for row in store.db.execute("SELECT report_json FROM grouping_runs ORDER BY rowid DESC"):
        report = json.loads(row["report_json"])
        parameters = report["parameters"]
        if (report["input_snapshot"] == str(snapshot)
                and parameters.get("normalization_algorithm") == algorithm
                and parameters.get("algorithm") == grouping.ALGORITHM_VERSION
                and parameters.get("implementation_sha256") == implementation):
            return report
    raise ValueError("Current normalization and grouping are required before building a split")


def build_split(store, test_fraction=.2):
    """Write a reproducible manifest using current, already-computed grouping.

    Labels remain provisional unless reviewed. This does not normalize or group
    mail, modify labels, or establish a validated final holdout.
    """
    if isinstance(test_fraction, bool) or not math.isfinite(test_fraction) or not 0 < test_fraction < 1:
        raise ValueError("test_fraction must be in (0,1)")
    snapshot = store.snapshot()
    algorithm = algorithm_identity()
    grouped = _grouping_run(store, snapshot, algorithm)
    normalized = {row["source_sha256"]: dict(row) for row in store.db.execute(
        "SELECT * FROM normalized_messages WHERE algorithm=?", (algorithm,))}
    latest = {(row["account"], row["sha256"]): dict(row) for row in store.db.execute("""
        SELECT r.id,r.account,r.sha256,r.label FROM reviews r
        WHERE r.id=(SELECT MAX(r2.id) FROM reviews r2
                    WHERE r2.account=r.account AND r2.sha256=r.sha256)
    """)}
    records = {}
    for entry in store.db.execute("""
        SELECT account,folder,uidvalidity,uid,sha256,internal_date,provisional_label,
               label_source,date_warning FROM entries
        ORDER BY account,sha256,folder,uidvalidity,uid
    """):
        source = dict(entry)
        key = source.pop("account"), source.pop("sha256")
        if key not in records:
            view = normalized.get(key[1])
            if view is None:
                raise ValueError("Normalization is incomplete")
            identity = json.loads(view["metadata"])["content_identity"]
            records[key] = {
                "account": key[0], "sha256": key[1], "source_sha256": key[1],
                "source_path": str(store.message_path(key[1])),
                "effective_sha256": view["effective_sha256"],
                "effective_path": str(store.effective_path(key[1], view["effective_sha256"])),
                "content_identity": identity, "normalization_status": view["status"],
                "sources": [], "group_ids": [],
            }
        records[key]["sources"].append(source)
    for key, record in records.items():
        review = latest.get(key)
        labels = {source["provisional_label"] for source in record["sources"]
                  if source["provisional_label"] is not None}
        record["label"] = review["label"] if review else next(iter(labels)) if len(labels) == 1 else "unsure"
        record["label_status"] = "reviewed" if review else "provisional"
        record["label_provisional"] = review is None
        record["review_id"] = review["id"] if review else None

    parents = {key: key for key in records}

    def find(key):
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    def union(left, right):
        left, right = find(left), find(right)
        if left != right:
            parents[max(left, right)] = min(left, right)

    identities = defaultdict(list)
    for key, record in records.items():
        identities[record["content_identity"]].append(key)
    for keys in identities.values():
        for key in keys[1:]:
            union(keys[0], key)
    memberships = defaultdict(list)
    for row in store.db.execute("""
        SELECT g.account,g.group_id,m.sha256 FROM corpus_groups g
        JOIN group_members m USING(run_id,group_id) WHERE g.run_id=?
        ORDER BY g.group_id,m.sha256
    """, (grouped["run_id"],)):
        key = row["account"], row["sha256"]
        if key not in records:
            raise ValueError("Grouping contains a source absent from the current corpus")
        records[key]["group_ids"].append(row["group_id"])
        memberships[row["group_id"]].append(key)
    for keys in memberships.values():
        for key in keys[1:]:
            union(keys[0], key)

    conflicts = set()
    for identity, keys in identities.items():
        labels = {records[key]["label"] for key in keys
                  if records[key]["normalization_status"] in ("unchanged", "unwrapped")}
        if len(labels) > 1:
            conflicts.add(identity)
    components = defaultdict(list)
    for key in records:
        components[find(key)].append(key)
    eligible, excluded = [], []
    buckets = defaultdict(list)
    for keys in components.values():
        component_id = _digest(sorted(keys))
        selected = []
        for key in keys:
            record = records[key]
            record["component_id"] = record["group_id"] = component_id
            reasons = []
            if record["normalization_status"] not in ("unchanged", "unwrapped"):
                reasons.append("unsafe_normalization")
            if record["content_identity"] in conflicts:
                reasons.append("effective_content_label_conflict")
            if record["label"] not in ("ham", "spam"):
                reasons.append("unresolved_label")
            if reasons:
                record["exclusion_reasons"] = reasons
                excluded.append(record)
            else:
                if not record["group_ids"]:
                    raise ValueError("Current grouping does not cover every eligible source")
                eligible.append(record)
                selected.append(record)
        if selected:
            dates = [date for record in selected for source in record["sources"]
                     if (date := _date(source)) is not None]
            labels = tuple(sorted({record["label"] for record in selected}))
            buckets[labels].append({
                "component_id": component_id, "records": selected,
                "latest_internal_date": max(dates, default=""),
            })
    # Select a recent prefix within each label stratum, never cutting a component.
    # A sole component stays in development; two or more retain both partitions.
    component_counts = Counter()
    for components_in_bucket in buckets.values():
        ordered = sorted(components_in_bucket, key=lambda item: (
            item["latest_internal_date"], item["component_id"]), reverse=True)
        target = sum(len(item["records"]) for item in ordered) * test_fraction
        cumulative = [0]
        for item in ordered:
            cumulative.append(cumulative[-1] + len(item["records"]))
        candidates = range(1, len(ordered)) if len(ordered) > 1 else (0,)
        cutoff = min(candidates, key=lambda index: (abs(cumulative[index] - target), index))
        for index, item in enumerate(ordered):
            partition = "test" if index < cutoff else "development"
            component_counts[partition] += 1
            for record in item["records"]:
                record["partition"] = partition
                record["component_latest_internal_date"] = item["latest_internal_date"] or None

    sort_key = lambda record: (record["account"], record["sha256"])
    parameters = {
        "algorithm": ALGORITHM_VERSION, "test_fraction": test_fraction,
        "normalization_algorithm": algorithm, "grouping_run_id": grouped["run_id"],
        "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    manifest = {
        "parameters": parameters, "input_snapshot": str(snapshot),
        "purpose": "provisional development/test; not a validated final holdout",
        "records": sorted(eligible, key=sort_key),
        "excluded_records": sorted(excluded, key=sort_key),
    }
    run_id = _digest(manifest)
    directory = store.root / "runs" / f"split-{run_id}"
    manifest_path = directory / "manifest.json"
    _write(manifest_path, manifest)
    counts = {partition: {
        "records": sum(record["partition"] == partition for record in eligible),
        "labels": dict(Counter(record["label"] for record in eligible if record["partition"] == partition)),
        "effective_identities": len({record["content_identity"] for record in eligible
                                    if record["partition"] == partition}),
        "components": component_counts[partition],
        "label_status": dict(Counter(record["label_status"] for record in eligible
                                     if record["partition"] == partition)),
        "source_distribution": _source_distribution(
            record for record in eligible if record["partition"] == partition),
    } for partition in ("development", "test")}
    report = {
        "run_id": run_id, "manifest_path": str(manifest_path),
        "report_path": str(directory / "report.json"),
        "input_snapshot": str(snapshot), "parameters": parameters, "counts": counts,
        "excluded_records": len(excluded),
        "exclusion_reasons": dict(Counter(reason for record in excluded
                                         for reason in record["exclusion_reasons"])),
        "effective_content_label_conflicts": len(conflicts),
        "labels_provisional": any(record["label_provisional"] for record in eligible),
        "limitations": [
            "This is a provisional development/test split, not a validated final holdout.",
            "Latest explicit reviews override folder labels; unreviewed labels remain provisional.",
            "Connected components join per-account groups and global effective-content identities.",
            "Recent component maximum internal dates determine test prefixes within label strata.",
            "Internal dates can reflect imports, copies, or migrations rather than receipt time.",
            "Missing or warned dates sort before valid dates; ties use stable component hashes.",
            "Whole components and label strata prevent exact requested fractions in small or skewed corpora.",
            "Heuristic grouping and content identity may miss other near-duplicate leakage.",
            "Benchmark sampling balances labels and deduplicates identities; it is not prevalence representative.",
        ],
    }
    report["benchmark"] = sample_test(store, report)
    report["benchmark_manifest_path"] = report["benchmark"]["manifest_path"]
    _write(Path(report["report_path"]), report)
    return report


def sample_test(store, split_report=None, *, per_label=1000, seed=0):
    """Write a seeded, identity-distinct test-only balanced smoke sample.

    Insufficient populations return status='insufficient' and manifest_path=None;
    no replacement, development borrowing, or partial sample is performed.
    """
    if isinstance(per_label, bool) or not isinstance(per_label, int) or per_label < 1:
        raise ValueError("per_label must be a positive integer")
    if not isinstance(seed, (str, int)) or isinstance(seed, bool):
        raise ValueError("seed must be an integer or string")
    if split_report is None:
        split_report = build_split(store)
    manifest_path = Path(split_report["manifest_path"])
    manifest = json.loads(manifest_path.read_text())
    unique = {}
    for record in sorted(manifest["records"], key=lambda item: (item["account"], item["sha256"])):
        if record["partition"] == "test" and record["label"] in ("ham", "spam"):
            unique.setdefault(record["content_identity"], record)
    populations = {label: [record for record in unique.values() if record["label"] == label]
                   for label in ("ham", "spam")}
    available = {label: len(records) for label, records in populations.items()}
    report = {
        "status": "insufficient", "manifest_path": None,
        "split_manifest_path": str(manifest_path), "seed": seed,
        "requested_per_label": per_label, "available": available,
        "selected": {"ham": 0, "spam": 0},
        "source_distribution": [], "label_status": {},
        "labels_provisional": None,
        "label_caveat": "Unreviewed source-folder labels remain provisional; this is a smoke sample.",
    }
    if any(count < per_label for count in available.values()):
        return report
    selected = []
    for label, population in populations.items():
        selected.extend(sorted(population, key=lambda record: (
            _digest([seed, record["content_identity"]]), record["account"], record["sha256"]
        ))[:per_label])
    selected.sort(key=lambda record: (record["label"], record["account"], record["sha256"]))
    path = manifest_path.parent / f"benchmark-{_digest([per_label, seed])}.json"
    _write(path, {
        "purpose": "balanced performance smoke evaluation; not a validated final holdout",
        "split_manifest_path": str(manifest_path), "seed": seed,
        "requested_per_label": per_label,
        "label_caveat": report["label_caveat"],
        "source_distribution": _source_distribution(selected),
        "records": selected,
    })
    report.update(status="ready", manifest_path=str(path),
                  selected={label: per_label for label in populations},
                  source_distribution=_source_distribution(selected),
                  label_status=dict(Counter(record["label_status"] for record in selected)),
                  labels_provisional=any(record["label_provisional"] for record in selected))
    return report

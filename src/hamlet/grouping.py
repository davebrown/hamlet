"""Conservative, local-only grouping proposals, never label corrections."""

from collections import Counter, defaultdict
from email import policy
from email.parser import BytesParser
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import platform
import re
import tempfile

from .diagnostics import event


ALGORITHM_VERSION = "threads-and-templates-v2-normalized"
MAX_RAW_BYTES = 5_000_000
MAX_TEXT_CHARS = 100_000
MAX_SHINGLES = 2_048
MIN_SHINGLES = 20
MAX_CANDIDATES = 4_096
PRIME = (1 << 61) - 1
PERMUTATIONS = [
    (int.from_bytes(hashlib.sha256(f"a:{i}".encode()).digest()[:8], "big") % (PRIME - 1) + 1,
     int.from_bytes(hashlib.sha256(f"b:{i}".encode()).digest()[:8], "big") % PRIME)
    for i in range(32)
]


class VisibleHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if not self.hidden:
            for name, value in attrs:
                if name == "href" and value:
                    self.parts.append(value)

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def inline_parts(message):
    if (message.get_content_disposition() == "attachment"
            or message.get_content_maintype() == "message"):
        return
    if message.is_multipart():
        for child in message.get_payload():
            yield from inline_parts(child)
    else:
        yield message


def body_text(path):
    warnings = set()
    if path.stat().st_size > MAX_RAW_BYTES:
        return "", {"raw_size_limit"}
    # Filesystem errors remain fatal; corrupted or missing corpus files need attention.
    raw = path.read_bytes()
    text = ""
    try:
        message = BytesParser(policy=policy.compat32).parsebytes(raw)
        if message.defects:
            warnings.add("mime_defect")
        plain, html = [], []
        for part in inline_parts(message):
            kind = part.get_content_type()
            if kind not in ("text/plain", "text/html"):
                continue
            try:
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    try:
                        value = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    except (LookupError, ValueError):
                        value = payload.decode("utf-8", errors="replace")
                        warnings.add("charset_fallback")
                else:
                    value = str(part.get_payload())
                if part.defects:
                    warnings.add("mime_defect")
                if len(value) > MAX_TEXT_CHARS:
                    warnings.add("text_truncated")
                (plain if kind == "text/plain" else html).append(value[:MAX_TEXT_CHARS])
            except Exception:
                warnings.add("mime_part_fallback")
        if plain:
            text = "\n".join(plain)
        elif html:
            renderer = VisibleHTML()
            try:
                renderer.feed("\n".join(html)[:MAX_TEXT_CHARS])
            except Exception:
                warnings.add("html_fallback")
            text = " ".join(renderer.parts)
    except Exception:
        warnings.add("mime_parse_failure")
    if len(text) > MAX_TEXT_CHARS:
        warnings.add("text_truncated")
    return text[:MAX_TEXT_CHARS], warnings


def fingerprint(text):
    text = text.casefold()
    text = re.sub(r"\b[\w.+-]+@([\w.-]+)", r"recipient@\1", text)
    text = re.sub(r"\b[0-9a-f]{16,}\b", "trackingtoken", text)
    text = re.sub(r"\b\d+\b", "numbertoken", text)
    words = re.findall(r"\w+", text)
    shingles = {
        int.from_bytes(hashlib.blake2b(
            " ".join(words[index:index + 5]).encode(), digest_size=8
        ).digest(), "big") % PRIME
        for index in range(max(0, len(words) - 4))
    }
    truncated = len(shingles) > MAX_SHINGLES
    digest = hashlib.sha256(" ".join(words).encode()).hexdigest()
    return digest, frozenset(shingles), truncated


def signature(features):
    # Bound candidate hashing; final similarity scores still use all retained shingles.
    features = sorted(features)[:MAX_SHINGLES]
    return tuple(min((a * value + b) % PRIME for value in features)
                 for a, b in PERMUTATIONS)


def bands(sig):
    return [(index // 4, sig[index:index + 4]) for index in range(0, len(sig), 4)]


def similarity(left, right):
    return len(left & right) / len(left | right)


def corpus_rows(store, algorithm):
    rows = store.db.execute("""
        SELECT e.account,e.sha256,e.provisional_label,
               m.message_id,m.in_reply_to,m.references_header,r.label AS reviewed_label
        FROM entries e JOIN messages m USING(sha256)
        LEFT JOIN reviews r ON r.id=(
            SELECT MAX(r2.id) FROM reviews r2
            WHERE r2.account=e.account AND r2.sha256=e.sha256)
        ORDER BY e.account,e.sha256
    """)
    messages = {}
    for row in rows:
        key = row["account"], row["sha256"]
        if key not in messages:
            messages[key] = {**dict(row), "folder_labels": set()}
        if row["provisional_label"]:
            messages[key]["folder_labels"].add(row["provisional_label"])
    conflicts = []
    for row in messages.values():
        if row["reviewed_label"] is not None:
            row["label"] = row["reviewed_label"]
        elif len(row["folder_labels"]) == 1:
            row["label"] = next(iter(row["folder_labels"]))
        else:
            row["label"] = "unsure"
        if len(row["folder_labels"]) > 1:
            conflicts.append({"account": row["account"], "sha256": row["sha256"]})
    normalized = {
        row["source_sha256"]: row for row in store.db.execute(
            "SELECT * FROM normalized_messages WHERE algorithm=?", (algorithm,))
    }
    identities = defaultdict(list)
    for row in messages.values():
        view = normalized.get(row["sha256"])
        if view is None:
            raise ValueError("Normalization is incomplete")
        metadata = json.loads(view["metadata"])
        row.update({name: metadata[name] for name in
                    ("message_id", "in_reply_to", "references_header")})
        row["effective_sha256"] = view["effective_sha256"]
        row["content_identity"] = metadata["content_identity"]
        row["normalization_status"] = view["status"]
        row["safe"] = view["status"] in ("unchanged", "unwrapped")
        if row["safe"]:
            identities[row["account"], row["content_identity"]].append(row)
    effective_conflicts = []
    for (account, identity), related in identities.items():
        labels = {row["label"] for row in related}
        if len(labels) > 1:
            effective_conflicts.append({
                "account": account, "content_identity": identity,
                "sources": sorted(row["sha256"] for row in related),
            })
            for row in related:
                row["safe"] = False
    return list(messages.values()), conflicts, effective_conflicts


def ham_threads(rows):
    parents = list(range(len(rows)))

    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    owners = {}
    for index, row in enumerate(rows):
        identity = row.get("content_identity")
        if identity:
            key = row["account"], "effective", identity
            if key in owners:
                parents[find(index)] = find(owners[key])
            else:
                owners[key] = index
        # Shared absent ancestors still link siblings. Subject alone never links mail.
        for field in ("message_id", "in_reply_to", "references_header"):
            ids = re.findall(r"<([^<>\s]{1,998})>", row[field] or "")
            for identifier in ids:
                key = row["account"], identifier
                if key in owners:
                    parents[find(index)] = find(owners[key])
                else:
                    owners[key] = index
    groups = defaultdict(list)
    for index, row in enumerate(rows):
        groups[row["account"], find(index)].append(
            {"sha256": row["sha256"], "evidence": "reference_headers", "similarity": None}
        )
    result = list(groups.values())
    for members in result:
        if len(members) == 1:
            members[0]["evidence"] = "singleton"
    return result


def spam_campaigns(store, rows, threshold):
    groups, anchor_features = [], []
    buckets = defaultdict(set)
    exact = {}
    effective_exact = {}
    warnings = Counter()
    comparisons = 0
    for index, row in enumerate(rows):
        digest = row["sha256"]
        effective = row.get("effective_sha256", digest)
        content_key = (row["account"], row.get("content_identity", digest))
        if content_key in effective_exact:
            target, score = effective_exact[content_key]
            groups[target].append(
                {"sha256": digest, "evidence": "effective_content", "similarity": score})
            continue
        text, parse_warnings = body_text(store.effective_path(digest, effective))
        warnings.update(parse_warnings)
        body_digest, features, truncated = fingerprint(text)
        if truncated:
            warnings["shingle_limit"] += 1
        enough = len(features) >= MIN_SHINGLES and not parse_warnings.intersection({
            "raw_size_limit", "text_truncated", "mime_parse_failure",
            "mime_part_fallback", "html_fallback",
        })
        identity = row["account"], body_digest
        target, score, evidence = None, None, "singleton"
        sig = None
        if enough and identity in exact:
            target, score, evidence = exact[identity], 1.0, "normalized_body"
            score = similarity(features, anchor_features[target])
        elif enough:
            sig = signature(features)
            candidates = set()
            for band in bands(sig):
                candidates.update(buckets[row["account"], band])
            if len(candidates) > MAX_CANDIDATES:
                warnings["candidate_limit"] += 1
            else:
                for candidate in sorted(candidates):
                    comparisons += 1
                    candidate_score = similarity(features, anchor_features[candidate])
                    if candidate_score >= threshold and (target is None or candidate_score > score):
                        target, score, evidence = candidate, candidate_score, "near_template"
        if len(features) < MIN_SHINGLES:
            warnings["insufficient_text"] += 1
        elif not enough:
            warnings["partial_body_excluded"] += 1
        if target is None:
            target = len(groups)
            groups.append([])
            anchor_features.append(features)
            if enough:
                sig = sig or signature(features)
                for band in bands(sig):
                    buckets[row["account"], band].add(target)
            score, evidence = 1.0, "anchor"
        groups[target].append({"sha256": digest, "evidence": evidence, "similarity": score})
        effective_exact[content_key] = target, score
        if enough:
            exact[identity] = target
        if (index + 1) % 500 == 0:
            event("grouping_progress", kind="spam_campaign", processed=index + 1, total=len(rows))
    return groups, dict(warnings), comparisons


def summarize(groups):
    sizes = Counter()
    for group in groups:
        size = len(group["members"])
        sizes["1" if size == 1 else "2-5" if size <= 5 else "6-20" if size <= 20 else "21+"] += 1
    return {
        "messages": sum(len(group["members"]) for group in groups),
        "groups": len(groups),
        "multi_message_groups": sum(len(group["members"]) > 1 for group in groups),
        "messages_in_multi_message_groups": sum(
            len(group["members"]) for group in groups if len(group["members"]) > 1),
        "group_size_distribution": dict(sizes),
        "largest_groups": [
            {"group_id": group["group_id"], "size": len(group["members"]),
             "anchor_sha256": group["anchor"],
             "minimum_anchor_similarity": min(
                 (member["similarity"] for member in group["members"]
                  if member["similarity"] is not None), default=None)}
            for group in sorted(groups, key=lambda group: (-len(group["members"]), group["group_id"]))[:10]
        ],
    }


def group_corpus(store, threshold=0.85):
    if not 0 < threshold <= 1:
        raise ValueError("Similarity threshold must be in (0,1]")
    from .normalized import normalize_corpus
    normalization = normalize_corpus(store)
    snapshot = store.snapshot()
    parameters = {
        "algorithm": ALGORITHM_VERSION, "spam_threshold": threshold,
        "normalization_algorithm": normalization["algorithm"],
        "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "python_version": platform.python_version(),
        "max_raw_bytes": MAX_RAW_BYTES, "max_text_chars": MAX_TEXT_CHARS,
        "max_shingles": MAX_SHINGLES, "min_shingles": MIN_SHINGLES,
        "minhash_permutations": len(PERMUTATIONS), "band_rows": 4,
        "max_candidates": MAX_CANDIDATES,
    }
    run_id = hashlib.sha256(
        (snapshot.stem + json.dumps(parameters, sort_keys=True)).encode()
    ).hexdigest()
    existing = store.db.execute(
        "SELECT report_json FROM grouping_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    if existing is not None:
        return json.loads(existing[0])
    rows, conflicts, effective_conflicts = corpus_rows(store, normalization["algorithm"])
    report = {
        "run_id": run_id, "input_snapshot": str(snapshot), "parameters": parameters,
        "population": "exported corpus snapshot; labels remain provisional unless reviewed",
        "normalization": normalization,
        "effective_label_conflicts": effective_conflicts,
        "excluded_unsafe_or_effective_conflict_messages": sum(not row["safe"] for row in rows),
        "distinct_safe_effective_messages": len({
            (row["account"], row["content_identity"]) for row in rows if row["safe"]
        }),
        "label_conflicts": conflicts,
        "excluded_unsure_messages": sum(row["label"] not in ("ham", "spam") for row in rows),
        "accounts": {},
        "limitations": [
            "References can be absent, incorrect, or forged; subject-only ham matches are not used.",
            "Spam groups are heuristic template candidates, not verified campaign identities.",
            "MinHash candidate retrieval is approximate; similar messages may remain separate.",
            "Unsafe report extractions are excluded; partial bodies remain singletons except effective-content duplicates.",
            "Effective-content identity normalizes safe textual CRLF only; binary/signature/parser uncertainty retains byte identity.",
            "Grouping is per account; future pooled splits must also protect cross-account duplicates.",
            "No labels or dataset partitions are changed; manually audit groups before splitting.",
        ],
    }
    all_groups = []
    for account in sorted({row["account"] for row in rows}):
        selected = [row for row in rows if row["account"] == account]
        ham = ham_threads([row for row in selected if row["label"] == "ham" and row["safe"]])
        spam, warnings, comparisons = spam_campaigns(
            store, [row for row in selected if row["label"] == "spam" and row["safe"]], threshold
        )
        summaries = {}
        for kind, members_list in (("ham_thread", ham), ("spam_campaign", spam)):
            groups = []
            for members in members_list:
                anchor = members[0]["sha256"]
                key = json.dumps([account, kind, sorted(member["sha256"] for member in members)])
                group = {"group_id": hashlib.sha256(key.encode()).hexdigest(),
                         "account": account, "kind": kind, "anchor": anchor, "members": members}
                groups.append(group)
            summaries[kind] = summarize(groups)
            all_groups.extend(groups)
        summaries["spam_parsing_and_limits"] = warnings
        summaries["spam_candidate_comparisons"] = comparisons
        report["accounts"][account] = summaries
    directory = store.root / "runs" / f"grouping-{run_id}"
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / "report.json"
    report["report_path"] = str(path)
    content = json.dumps(report, indent=2, sort_keys=True)
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".report-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content + "\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    with store.db:
        store.db.execute(
            "INSERT INTO grouping_runs(run_id,snapshot,parameters,report_json) VALUES (?,?,?,?)",
            (run_id, str(snapshot), json.dumps(parameters, sort_keys=True), content),
        )
        for group in all_groups:
            store.db.execute(
                """INSERT INTO corpus_groups(run_id,group_id,account,kind,anchor_sha256)
                   VALUES (?,?,?,?,?)""",
                (run_id, group["group_id"], group["account"], group["kind"], group["anchor"]),
            )
            store.db.executemany(
                """INSERT INTO group_members(run_id,group_id,sha256,evidence,similarity)
                   VALUES (?,?,?,?,?)""",
                [(run_id, group["group_id"], member["sha256"], member["evidence"], member["similarity"])
                 for member in group["members"]],
            )
    return report

"""Private, sequential hosted benchmark orchestration (provisional, not a release gate)."""

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import time
from uuid import uuid4

from . import prompts, providers
from .config import read_environment
from .evaluation import upper_binomial
from .providers import MODEL_SPECS, ProviderError, classify
from .store import private_directory


TOTAL = 2000
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens",
                "uncached_input_tokens", "cache_write_input_tokens",
                "cache_write_short_tokens", "cache_write_long_tokens")
RESULT_FIELDS = (*TOKEN_FIELDS, "estimated_cost_usd", "usage_complete",
                 "returned_model", "stop_reason", "truncated", "cost_is_lower_bound")
TRUNCATED = {"length", "max_tokens", "max_output_tokens", "incomplete", "model_context_window_exceeded"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _hash_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    _sync_directory(path.parent)


@contextmanager
def _lock(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("A hosted benchmark writer is already active") from None
        yield
    finally:
        os.close(fd)


def _validate_manifest(manifest_path, root):
    content = Path(manifest_path).expanduser().resolve().read_bytes()
    samples = json.loads(content)["records"]
    if Counter(row["label"] for row in samples) != Counter(ham=1000, spam=1000):
        raise ValueError("Hosted benchmark requires exactly 1000 ham and 1000 spam")
    identities = set()
    for sample in samples:
        if sample.get("partition") != "test":
            raise ValueError("Hosted benchmark requires the test partition")
        source = sample["sha256"]
        if sample.get("source_sha256", source) != source:
            raise ValueError("Benchmark source hashes disagree")
        identity = sample.get("content_identity", sample["effective_sha256"])
        if identity in identities:
            raise ValueError("Duplicate benchmark content identity")
        identities.add(identity)
        for field, digest in (("source_path", source),
                              ("effective_path", sample["effective_sha256"])):
            if (not isinstance(digest, str) or len(digest) != 64
                    or any(char not in "0123456789abcdef" for char in digest)):
                raise ValueError("Invalid benchmark content hash")
            fallback = root / "messages" / source[:2] / f"{source}.eml"
            path = Path(sample.get(field, fallback) if field == "source_path"
                        else sample[field]).expanduser().resolve()
            if not path.is_relative_to(root):
                raise ValueError("Benchmark mail path is outside private data root")
            if _hash_file(path) != digest:
                raise ValueError("Benchmark content integrity failure")
    return samples, hashlib.sha256(content).hexdigest()


def _safe_result(result):
    """Keep usage even for invalid responses; never persist response text."""
    safe = {}
    for name in (*TOKEN_FIELDS, "estimated_cost_usd"):
        value = result.get(name)
        if value is None:
            safe[name] = None
            continue
        if (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0
                and (name == "estimated_cost_usd" or isinstance(value, int))):
            safe[name] = value
    safe["usage_complete"] = (
        result.get("usage_complete") is True
        and all(safe.get(name) is not None
                for name in ("input_tokens", "output_tokens", "estimated_cost_usd")))
    safe["truncated"] = result.get("truncated") is True
    safe["cost_is_lower_bound"] = (
        result.get("cost_is_lower_bound") is True or not safe["usage_complete"])
    for name in ("returned_model", "stop_reason"):
        value = result.get(name)
        if value is None or isinstance(value, str):
            safe[name] = value
    safe["predicted"] = (
        result.get("predicted") if result.get("predicted") in ("ham", "spam") else "invalid")
    return safe


def _quality_failure(row, index):
    if row.get("blocked_reason"):
        return row["blocked_reason"]
    if not row["usage_complete"]:
        return "missing_usage"
    if row.get("truncated") or row.get("stop_reason") in TRUNCATED:
        return "truncated_response"
    if index == 0 and row["predicted"] == "invalid":
        return "invalid_preflight"
    return None


def _failure(error):
    return {
        "status_code": error.status_code if type(error.status_code) is int else None,
        "ambiguous": error.ambiguous is not False,
        "retryable": error.retryable is True,
        **{name: getattr(error, name, None)
           for name in ("message", "cause", "error_code", "parameter", "retry_after")},
    }


def _record_attempt(directory, failure, result=None, prior_report=None):
    """Make uncertain work durable before authorizing another request."""
    flight_path = directory / "in-flight.json"
    flight = json.loads(flight_path.read_text())
    attempt_id = flight.get("attempt_id") or _digest(flight)
    if (not isinstance(attempt_id, str) or not attempt_id
            or any(char not in "0123456789abcdef" for char in attempt_id)):
        raise ValueError("Invalid attempt identity")
    path = directory / f"attempt-{attempt_id}.json"
    if not path.exists():
        attempt = {
            "request": flight, "ended_at": _now(), "failure": failure,
            "may_be_billed": failure.get("ambiguous") is not False or result is not None,
            "result": _safe_result(result) if isinstance(result, dict) else None,
        }
        if prior_report is not None:
            attempt["prior_report"] = prior_report
            attempt["retry_authorization"] = "Explicit --resilient invocation"
        _atomic(path, attempt)
    flight_path.unlink()
    _sync_directory(directory)


def _read_attempts(directory):
    return [json.loads(path.read_text()) for path in sorted(directory.glob("attempt-*.json"))]


def summarize_hosted(records, elapsed, attempts=()):
    labels = Counter(row["expected"] for row in records)
    confusion = Counter((row["expected"], row["predicted"]) for row in records)
    count = len(records)
    request_seconds = sum(row["wall_seconds"] for row in records)
    billed_attempts = [row for row in attempts if row["may_be_billed"]]
    usage_rows = [*records, *(row["result"] or {} for row in billed_attempts)]
    totals = {name: sum(row.get(name) or 0 for row in usage_rows) for name in TOKEN_FIELDS}
    totals["responses_missing_counter"] = {
        name: sum(row.get(name) is None for row in usage_rows) for name in TOKEN_FIELDS}
    if any(row.get("reasoning_tokens") is None for row in usage_rows):
        totals["known_reasoning_tokens"] = totals["reasoning_tokens"]
        totals["reasoning_tokens"] = None
    totals["responses_missing_usage"] = sum(
        not row.get("usage_complete", False) for row in usage_rows)
    false_positives = confusion["ham", "spam"]
    return {
        "completed": count, "total": TOTAL, "labels": dict(labels),
        "confusion": {f"{actual}_as_{predicted}": number
                      for (actual, predicted), number in sorted(confusion.items())},
        "invalid_or_failed": sum(row["predicted"] == "invalid" for row in records),
        "ham_false_positive_rate": (
            false_positives / labels["ham"] if labels["ham"] else None),
        "ham_fpr_one_sided_95_upper": upper_binomial(false_positives, labels["ham"]),
        "spam_recall": confusion["spam", "spam"] / labels["spam"] if labels["spam"] else None,
        "tokens": totals,
        "cost": {
            "estimated_cost_usd": sum(row.get("estimated_cost_usd") or 0 for row in usage_rows),
            "usage_complete": all(row.get("usage_complete", False) for row in usage_rows),
            "unknown_usage_attempts": sum(
                not (row["result"] or {}).get("usage_complete", False) for row in billed_attempts),
            "estimate_may_omit_billed_requests": any(
                not (row["result"] or {}).get("usage_complete", False) for row in billed_attempts),
            "basis": "Known provider-reported usage at frozen prices; excludes unknown attempt charges.",
        },
        "failed_request_attempts": len(attempts),
        "timing": {
            "wall_seconds": elapsed, "request_wall_seconds": request_seconds,
            "mean_request_seconds": request_seconds / count if count else None,
            "wall_seconds_per_email": elapsed / count if count else None,
            "wall_emails_per_second": count / elapsed if elapsed else None,
        },
    }


def _read_prefix(directory, samples, run_id):
    records = []
    paths = sorted(directory.glob("row-*.json"))
    for index, path in enumerate(paths):
        if path.name != f"row-{index:04d}.json" or index >= len(samples):
            raise ValueError("Persisted hosted journal is not a benchmark prefix")
        row = json.loads(path.read_text())
        sample = samples[index]
        if (row.get("index"), row.get("run_id"), row.get("sha256"),
                row.get("effective_sha256"), row.get("expected")) != (
                index, run_id, sample["sha256"], sample["effective_sha256"], sample["label"]):
            raise ValueError("Persisted hosted predictions differ from benchmark")
        if (row.get("predicted") not in ("ham", "spam", "invalid")
                or not isinstance(row.get("wall_seconds"), (int, float))
                or not math.isfinite(row["wall_seconds"]) or row["wall_seconds"] < 0
                or not isinstance(row.get("usage_complete"), bool)
                or _safe_result(row) != {key: value for key, value in row.items()
                                         if key in (*RESULT_FIELDS, "predicted")}):
            raise ValueError("Invalid persisted hosted prediction metadata")
        records.append(row)
    return records


def _append_log(path, report):
    if not report["complete"]:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock(path.with_name(path.name + ".lock")):
        content = path.read_text() if path.exists() else ""
        if f'`{report["run_id"]}`' in content:
            return
        lines = content.splitlines(keepends=True)
        header = next((index for index, line in enumerate(lines)
                       if line.startswith("| Run timestamp (start, UTC) |")), None)
        if header is None:
            raise ValueError("Evaluation log is missing the evaluation table")
        insertion = header + 2
        while insertion < len(lines) and lines[insertion].startswith("|"):
            insertion += 1
        matrix = report["confusion"]
        hours, minutes = divmod(int(report["timing"]["wall_seconds"] / 60 + .5), 60)
        model = report["parameters"]["model"].replace("|", "").replace("`", "")
        uncertainty = ("; plus unknown attempt charges"
                       if not report["cost"]["usage_complete"] else "")
        row = (
            f'| {report["started_at"][:19].replace("T", " ")} | hosted smoke test | '
            f'`{model}` | 1,000 ham + 1,000 spam | {hours} hr {minutes} min | '
            f'{report["timing"]["mean_request_seconds"]:.3f} sec | '
            f'{report["tokens"]["input_tokens"]:,} | {report["tokens"]["output_tokens"]:,} | '
            f'${report["cost"]["estimated_cost_usd"]:.2f} (estimated{uncertainty}) | '
            f'{report["spam_recall"]:.1%} | {report["ham_false_positive_rate"]:.1%} | '
            f'{matrix.get("spam_as_spam", 0)} | {matrix.get("ham_as_spam", 0)} | '
            f'{matrix.get("ham_as_ham", 0)} | {matrix.get("spam_as_ham", 0)} | '
            f'{report["invalid_or_failed"]} | `{report["run_id"]}` |\n')
        lines.insert(insertion, row)
        temporary = path.with_name(path.name + ".tmp")
        with temporary.open("w") as stream:
            stream.write("".join(lines))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)


def _validate_log(path):
    content = Path(path).read_text()
    if not any(line.startswith("| Run timestamp (start, UTC) |")
               for line in content.splitlines()):
        raise ValueError("Evaluation log is missing the evaluation table")


def _compatible_parameters(runs, parameters):
    identity = ("manifest_sha256", "prompt_version", "prompt_implementation",
                "system_prompt_sha256", "max_chars", "per_label", "model", "spec")
    matches = []
    for path in runs.glob("hosted-*/parameters.json"):
        previous = json.loads(path.read_text())
        if all(previous.get(name) == parameters[name] for name in identity):
            if path.parent.name != "hosted-" + _digest(previous):
                raise ValueError("Persisted hosted parameter identity mismatch")
            matches.append(previous)
    if len(matches) > 1:
        raise ValueError("Multiple compatible runs; select one explicitly before recovery")
    return matches[0] if matches else parameters


def evaluate(manifest_path, data_root, *, models=None,
             evaluation_log="docs/evaluations.md", environment=None, resilient=False):
    """Run selected models sequentially, with one outstanding paid request at most."""
    root = Path(data_root).expanduser().resolve()
    runs = private_directory(root / "runs")
    selected = list(MODEL_SPECS) if models is None else list(dict.fromkeys(models))
    if not selected or any(model not in MODEL_SPECS for model in selected):
        raise ValueError("Select known hosted model IDs")
    if prompts.PROMPT_VERSION != "spam-v1":
        raise ValueError("Hosted benchmark requires spam-v1")
    environment = {} if environment is None else environment
    with _lock(runs / "hosted-writer.lock"):
        samples, manifest_hash = _validate_manifest(manifest_path, root)
        _validate_log(evaluation_log)
        common = {
            "manifest_sha256": manifest_hash, "prompt_version": prompts.PROMPT_VERSION,
            "prompt_implementation": _hash_file(prompts.__file__),
            "system_prompt_sha256": hashlib.sha256(prompts.SPAM_SYSTEM_PROMPT.encode()).hexdigest(),
            "runner_implementation": _hash_file(__file__),
            "provider_implementation": _hash_file(providers.__file__),
            "max_chars": 4000, "per_label": 1000, "model_concurrency": 1,
            "requests_per_model_concurrency": 1, "safe_429_backoff_seconds": [1, 2],
        }
        parameters = {model: {**common, "model": model, "spec": MODEL_SPECS[model]}
                      for model in selected}
        if resilient:
            parameters = {model: _compatible_parameters(runs, value)
                          for model, value in parameters.items()}
        run_ids = {model: _digest(parameters[model]) for model in selected}
        job_id = _digest(run_ids)
        progress_path = runs / "hosted-progress.json"
        progress = {
            "run_id": job_id, "status": "running", "started_at": _now(), "ended_at": None,
            "model_concurrency": 1, "models": {},
        }
        if progress_path.exists():
            previous_progress = json.loads(progress_path.read_text())
            if previous_progress.get("run_id") == job_id:
                progress["started_at"] = previous_progress["started_at"]
        reports = {}
        contexts = {}
        for model in selected:
            run_id = run_ids[model]
            directory = private_directory(runs / f"hosted-{run_id}")
            parameter_path = directory / "parameters.json"
            if parameter_path.exists() and json.loads(parameter_path.read_text()) != parameters[model]:
                raise ValueError("Persisted hosted settings mismatch")
            _atomic(parameter_path, parameters[model])
            execution = {
                "started_at": _now(), "runner_implementation": common["runner_implementation"],
                "provider_implementation": common["provider_implementation"],
                "resilient": resilient, "max_attempts": 5 if resilient else 3,
                "backoff_seconds": [2, 4, 8, 16] if resilient else [1, 2],
            }
            _atomic(directory / f"execution-{uuid4().hex}.json", execution)
            records = _read_prefix(directory, samples, run_id)
            report_path = directory / "report.json"
            previous = json.loads(report_path.read_text()) if report_path.exists() else {}
            if previous and (previous.get("run_id") != run_id
                             or previous.get("parameters") != parameters[model]
                             or previous.get("completed", 0) > len(records)):
                raise ValueError("Persisted hosted report mismatch")
            elapsed = max(previous.get("timing", {}).get("wall_seconds", 0),
                          sum(row["wall_seconds"] for row in records))
            report = {
                **summarize_hosted(records, elapsed, _read_attempts(directory)), "run_id": run_id,
                "parameters": parameters[model], "started_at": previous.get("started_at"),
                "ended_at": previous.get("ended_at"), "updated_at": _now(),
                "status": previous.get("status", "pending"), "complete": False,
            }
            if previous.get("blocked_reason"):
                report["blocked_reason"] = previous["blocked_reason"]
            if previous.get("failure"):
                failure = previous["failure"]
                report["failure"] = {
                    "status_code": failure.get("status_code")
                    if type(failure.get("status_code")) is int else None,
                    "ambiguous": failure.get("ambiguous") is not False,
                }
            flight_path = directory / "in-flight.json"
            if flight_path.exists():
                flight = json.loads(flight_path.read_text())
                index = flight.get("index")
                if (flight.get("run_id") != run_id or type(index) is not int
                        or not 0 <= index < len(samples)
                        or flight.get("sha256") != samples[index]["sha256"]
                        or index > len(records)):
                    raise ValueError("Invalid hosted in-flight marker")
                if index == len(records):
                    if resilient:
                        _record_attempt(directory, {
                            **previous.get("failure", {}),
                            "ambiguous": True, "message": "recovered_in_flight",
                        }, prior_report=previous)
                        report.update(summarize_hosted(records, elapsed, _read_attempts(directory)))
                        report.update(status="pending", ended_at=None)
                        report.pop("blocked_reason", None)
                        report.pop("failure", None)
                    else:
                        report.update(status="blocked", blocked_reason="uncertain_in_flight")
                else:
                    flight_path.unlink()
                    _sync_directory(directory)
            if (resilient and report.get("blocked_reason") in (
                    "ambiguous_provider_failure", "uncertain_in_flight", "transient_retries_exhausted")
                    and not flight_path.exists()):
                report.update(status="pending", ended_at=None)
                report.pop("blocked_reason", None)
                report.pop("failure", None)
            for index, row in enumerate(records):
                reason = _quality_failure(row, index)
                if reason:
                    report.update(status="blocked", blocked_reason=reason)
                    break
            if len(records) == len(samples) and report["status"] != "blocked":
                report.update(status="complete", complete=True,
                              ended_at=report["ended_at"] or records[-1]["ended_at"])
            reports[model] = report
            contexts[model] = (directory, records)

        def persist(model):
            report = reports[model]
            report["updated_at"] = _now()
            unresolved = (contexts[model][0] / "in-flight.json").exists()
            report["unresolved_requests"] = int(unresolved)
            report["cost"]["estimate_may_omit_billed_requests"] = (
                bool(unresolved) or report["cost"].get("unknown_usage_attempts", 0) > 0)
            if unresolved:
                report["cost"]["usage_complete"] = False
            _atomic(contexts[model][0] / "report.json", report)
            progress["models"][model] = {
                "completed": report["completed"], "total": TOTAL,
                "status": report["status"], "run_id": report["run_id"],
                "input_tokens": report["tokens"]["input_tokens"],
                "output_tokens": report["tokens"]["output_tokens"],
                "estimated_cost_usd": report["cost"]["estimated_cost_usd"],
                "usage_complete": report["cost"]["usage_complete"],
                "unknown_usage_attempts": report["cost"].get("unknown_usage_attempts", 0),
                "failed_request_attempts": report.get("failed_request_attempts", 0),
                "elapsed_seconds": report["timing"]["wall_seconds"],
                "started_at": report["started_at"], "ended_at": report["ended_at"],
            }
            if report.get("blocked_reason"):
                progress["models"][model]["blocked_reason"] = report["blocked_reason"]
            if report.get("failure"):
                progress["models"][model]["failure"] = report["failure"]
            progress["completed"] = sum(row["completed"] for row in progress["models"].values())
            progress["total"] = TOTAL * len(selected)
            for name in ("input_tokens", "output_tokens", "estimated_cost_usd"):
                progress[name] = sum(row[name] for row in progress["models"].values())
            progress["usage_complete"] = all(
                row["usage_complete"] for row in progress["models"].values())
            progress["updated_at"] = _now()
            _atomic(progress_path, progress)

        # All models are visible before the first request can leave this process.
        for model in selected:
            persist(model)
        blocked = None if resilient else next(
            (model for model in selected if reports[model]["status"] == "blocked"), None)
        for model in selected:
            if blocked:
                break
            report = reports[model]
            directory, records = contexts[model]
            if resilient and report["status"] == "blocked":
                continue
            if report["complete"]:
                _append_log(evaluation_log, report)
                continue
            report.update(status="running", started_at=report["started_at"] or _now())
            session_started = time.monotonic()
            previous_elapsed = report["timing"]["wall_seconds"]
            persist(model)
            for index in range(len(records), len(samples)):
                sample = samples[index]
                raw = Path(sample["effective_path"]).expanduser().resolve().read_bytes()
                if hashlib.sha256(raw).hexdigest() != sample["effective_sha256"]:
                    report.update(status="blocked", blocked_reason="content_changed")
                    blocked = model
                    break
                user = prompts.render_email(raw, max_chars=4000)
                request_seconds = 0
                flight_path = directory / "in-flight.json"
                row_result = None
                max_attempts = 5 if resilient else 3
                for attempt in range(max_attempts):
                    marker = {"run_id": report["run_id"], "index": index,
                              "sha256": sample["sha256"], "started_at": _now(),
                              "attempt_id": uuid4().hex}
                    _atomic(flight_path, marker)
                    call_started = time.monotonic()
                    try:
                        result = classify(model, prompts.SPAM_SYSTEM_PROMPT, user, environment)
                        request_seconds += time.monotonic() - call_started
                        row_result = _safe_result(result)
                        report.pop("failure", None)
                        report.pop("next_retry_at_seconds", None)
                        break
                    except ProviderError as error:
                        request_seconds += time.monotonic() - call_started
                        status = getattr(error, "status_code", None)
                        ambiguous = getattr(error, "ambiguous", True) is not False
                        error_result = getattr(error, "result", None)
                        if resilient:
                            failure = _failure(error)
                            _record_attempt(directory, failure, error_result)
                            report["failure"] = failure
                            can_retry = (
                                getattr(error, "retryable", False)
                                and (status is None or status in (408, 409, 429) or status >= 500))
                            if can_retry and attempt < max_attempts - 1:
                                report.update(summarize_hosted(
                                    records, previous_elapsed + time.monotonic() - session_started,
                                    _read_attempts(directory)))
                                report["status"] = "retrying"
                                delay = max(2 ** (attempt + 1),
                                            getattr(error, "retry_after", None) or 0)
                                report["next_retry_at_seconds"] = delay
                                persist(model)
                                time.sleep(delay)
                                report["status"] = "running"
                                continue
                            report.update(status="blocked", blocked_reason=(
                                "transient_retries_exhausted" if can_retry else "provider_rejected"))
                            blocked = model
                            break
                        if (status == 429 and getattr(error, "retryable", False)
                                and not ambiguous and not error_result and attempt < 2):
                            flight_path.unlink()
                            _sync_directory(directory)
                            time.sleep(2 ** attempt)
                            continue
                        report.update(status="blocked", blocked_reason=(
                            "ambiguous_provider_failure" if ambiguous else "provider_rejected"),
                            failure={"status_code": status if type(status) is int else None,
                                     "ambiguous": ambiguous})
                        # A response with usage must be journaled even when the provider failed.
                        row_result = _safe_result(error_result) if isinstance(error_result, dict) else None
                        if row_result is not None:
                            row_result["predicted"] = "invalid"
                        blocked = model
                        break
                    except Exception:
                        request_seconds += time.monotonic() - call_started
                        report.update(status="blocked", blocked_reason="ambiguous_provider_failure")
                        row_result = None
                        blocked = model
                        break
                if row_result is not None:
                    row = {
                        **row_result, "index": index, "run_id": report["run_id"],
                        "sha256": sample["sha256"], "effective_sha256": sample["effective_sha256"],
                        "expected": sample["label"], "started_at": marker["started_at"],
                        "ended_at": _now(), "wall_seconds": request_seconds,
                        "request_attempts": attempt + 1,
                    }
                    if blocked:
                        row["blocked_reason"] = report["blocked_reason"]
                    _atomic(directory / f"row-{index:04d}.json", row)
                    records.append(row)
                    # The row is durable before clearing uncertainty; a crash between is reconcilable.
                    flight_path.unlink()
                    _sync_directory(directory)
                    reason = _quality_failure(row, index)
                    if reason and not blocked:
                        report.update(status="blocked", blocked_reason=reason)
                        blocked = model
                report.update(summarize_hosted(
                    records, previous_elapsed + time.monotonic() - session_started,
                    _read_attempts(directory)))
                persist(model)
                if blocked:
                    break
            report.update(summarize_hosted(
                records, previous_elapsed + time.monotonic() - session_started,
                _read_attempts(directory)))
            if not blocked:
                report.update(status="complete", complete=True, ended_at=_now())
            else:
                report["ended_at"] = _now()
            persist(model)
            if report["complete"]:
                _append_log(evaluation_log, report)
            if resilient:
                blocked = None
        blocked = next((model for model in selected if reports[model]["status"] == "blocked"), None)
        progress.update(status="blocked" if blocked else "complete",
                        ended_at=_now(), updated_at=_now())
        if blocked:
            progress["blocked_model"] = blocked
            progress["action_required"] = (
                "Stop: review private journal, provider billing and configuration before any retry. "
                "Uncertain requests are never automatically reissued.")
        _atomic(progress_path, progress)
        return {**progress, "complete": not bool(blocked), "reports": reports}


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--model", action="append", choices=list(MODEL_SPECS))
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--evaluation-log", type=Path, default=Path("docs/evaluations.md"))
    parser.add_argument(
        "--resilient", action="store_true",
        help="Resume compatible runs and retry transient/uncertain requests up to 5 times; "
             "may duplicate unknown billed work, which is tracked separately. Continue other models.")
    args = parser.parse_args(argv)
    try:
        environment = read_environment()
        result = evaluate(
            args.manifest, args.data_root or environment.get("HAMLET_DATA_ROOT", "~/hamlet-data"),
            models=args.model, evaluation_log=args.evaluation_log, environment=environment,
            resilient=args.resilient)
    except Exception:
        # Exception strings/tracebacks may contain request payloads or credentials.
        print("Hosted benchmark stopped. Check private artifacts and configuration; no automatic retry.")
        return 1
    print(json.dumps({key: value for key, value in result.items() if key != "reports"}, indent=2))
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

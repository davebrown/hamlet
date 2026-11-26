"""Private, resumable Ollama performance smoke tests, not release evaluations."""

import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import time
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

from .diagnostics import event, failure_fields
from .config import read_environment
from .store import private_directory


def local_json(base_url, path, payload=None, timeout=180):
    parsed = urlparse(base_url)
    if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
            or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        raise ValueError("Only a local HTTP Ollama endpoint is permitted")
    request = Request(
        base_url.rstrip("/") + path,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
        return json.load(response)


def upper_binomial(errors, total, alpha=0.05):
    if total == 0:
        return None
    if errors == total:
        return 1.0
    if errors == 0:
        return 1 - alpha ** (1 / total)
    low, high = errors / total, 1.0
    for _ in range(60):
        probability = (low + high) / 2
        terms = [
            math.lgamma(total + 1) - math.lgamma(k + 1) - math.lgamma(total - k + 1)
            + k * math.log(probability) + (total - k) * math.log1p(-probability)
            for k in range(errors + 1)
        ]
        maximum = max(terms)
        cdf = math.exp(maximum) * sum(math.exp(term - maximum) for term in terms)
        if cdf > alpha:
            low = probability
        else:
            high = probability
    return (low + high) / 2


def summarize(records, wall_seconds, *, input_rate=None, output_rate=None):
    labels = Counter(row["expected"] for row in records)
    confusion = Counter((row["expected"], row["predicted"]) for row in records)
    invalid = sum(row["predicted"] not in ("ham", "spam") for row in records)
    prompt_tokens = sum(row.get("prompt_eval_count", 0) for row in records)
    completion_tokens = sum(row.get("eval_count", 0) for row in records)
    cached_tokens = sum(row.get("prompt_eval_cached_count", 0) for row in records)
    false_positives = confusion["ham", "spam"]
    recall = confusion["spam", "spam"] / labels["spam"] if labels["spam"] else None
    by_label = {}
    for label in ("ham", "spam"):
        selected = [row for row in records if row["expected"] == label]
        count = len(selected)
        by_label[label] = {
            "messages": count,
            "mean_request_seconds": (
                sum(row["wall_seconds"] for row in selected) / count if count else None),
            "prompt_tokens": sum(row.get("prompt_eval_count", 0) for row in selected),
            "generated_tokens": sum(row.get("eval_count", 0) for row in selected),
        }
    return {
        "completed": len(records), "labels": dict(labels),
        "confusion": {f"{actual}_as_{predicted}": count
                      for (actual, predicted), count in sorted(confusion.items())},
        "invalid_or_failed": invalid,
        "ham_false_positive_rate": false_positives / labels["ham"] if labels["ham"] else None,
        "ham_fpr_one_sided_95_upper": upper_binomial(false_positives, labels["ham"]),
        "spam_recall": recall,
        "timing": {
            "wall_seconds": wall_seconds,
            "request_wall_seconds": sum(row["wall_seconds"] for row in records),
            "ollama_total_seconds": sum(row.get("total_duration", 0) for row in records) / 1e9,
            "load_seconds": sum(row.get("load_duration", 0) for row in records) / 1e9,
            "prompt_eval_seconds": sum(row.get("prompt_eval_duration", 0) for row in records) / 1e9,
            "generation_seconds": sum(row.get("eval_duration", 0) for row in records) / 1e9,
            "seconds_per_email": wall_seconds / len(records) if records else None,
        },
        "tokens": {"prompt_eval_count": prompt_tokens, "eval_count": completion_tokens,
                   "prompt_eval_cached_count": cached_tokens,
                   "prompt_plus_generation": prompt_tokens + completion_tokens,
                   "responses_missing_usage": sum(
                       "prompt_eval_count" not in row or "eval_count" not in row for row in records)},
        "by_label": by_label,
        "projection_3000_emails_same_balanced_mix": {
            "sequential_seconds": wall_seconds * 3000 / len(records) if records else None,
            "prompt_tokens": prompt_tokens * 3000 / len(records) if records else None,
            "generated_tokens": completion_tokens * 3000 / len(records) if records else None,
        },
        "cost": {
            "local_api_charge_usd": 0,
            "electricity_and_hardware_cost": "not measured",
            "hypothetical_input_usd_per_million": input_rate,
            "hypothetical_output_usd_per_million": output_rate,
            "hypothetical_api_usd": (
                (prompt_tokens * input_rate + completion_tokens * output_rate) / 1e6
                if input_rate is not None and output_rate is not None else None),
        },
        "limitations": [
            "Performance smoke test using provisional labels, not a certified holdout or release gate.",
            "Invalid outputs and request errors are system failures; inspect them alongside accuracy.",
            "Email rendering is bounded; long bodies and attachments are not fully classified.",
            "Token counts are Ollama counters; cached tokens are separate, not assumed billable.",
            "Provider tokenization and prices differ; local timing does not predict hosted latency.",
            "Timed-out server work has unknown token usage; request failures can undercount tokens.",
        ],
    }


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def evaluate(manifest_path, data_root, *, model="llama3:latest",
             base_url="http://127.0.0.1:11434", per_label=1000,
             max_chars=4000, num_ctx=2048, num_predict=32,
             input_rate=None, output_rate=None):
    from . import prompts

    if min(per_label, max_chars, num_ctx, num_predict) <= 0:
        raise ValueError("Sample and context limits must be positive")
    if any(rate is not None and (not math.isfinite(rate) or rate < 0)
           for rate in (input_rate, output_rate)):
        raise ValueError("Hypothetical token prices must be finite and nonnegative")
    manifest_path = Path(manifest_path).expanduser().resolve()
    content = manifest_path.read_bytes()
    manifest = json.loads(content)
    samples = manifest["records"]
    counts = Counter(row["label"] for row in samples)
    if counts != Counter({"ham": per_label, "spam": per_label}):
        raise ValueError("Benchmark requires the requested number of each label")
    identities = set()
    data_root = Path(data_root).expanduser().resolve()
    for row in samples:
        if row.get("partition") != "test":
            raise ValueError("Benchmark input must be from the provisional test partition")
        identity = row.get("content_identity", row["effective_sha256"])
        if identity in identities:
            raise ValueError("Duplicate effective content in benchmark")
        identities.add(identity)
        path = Path(row["effective_path"]).resolve()
        if not path.is_relative_to(data_root):
            raise ValueError("Benchmark mail path is outside private data root")
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["effective_sha256"]:
            raise ValueError("Benchmark effective content integrity failure")
    tags = local_json(base_url, "/api/tags")
    selected = next((entry for entry in tags["models"] if entry["name"] == model), None)
    if selected is None:
        raise ValueError("Selected model is not installed")
    parameters = {
        "model": model, "model_digest": selected["digest"],
        "prompt_version": prompts.PROMPT_VERSION,
        "prompt_implementation": hashlib.sha256(Path(prompts.__file__).read_bytes()).hexdigest(),
        "runner_implementation": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "manifest_sha256": hashlib.sha256(content).hexdigest(),
        "max_chars": max_chars, "num_ctx": num_ctx, "num_predict": num_predict,
        "temperature": 0, "seed": 42, "per_label": per_label,
        "input_rate": input_rate, "output_rate": output_rate,
    }
    run_id = hashlib.sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest()
    directory = private_directory(data_root / "runs" / f"eval-{run_id}")
    lock = directory / "writer.lock"
    lock_fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    import fcntl
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        raise ValueError("This evaluation already has an active writer") from None
    try:
        return _run(samples, parameters, directory, run_id, prompts, base_url,
                    input_rate, output_rate)
    finally:
        os.close(lock_fd)


def _run(samples, parameters, directory, run_id, prompts, base_url, input_rate, output_rate):
    atomic_json(directory / "parameters.json", parameters)
    predictions_path = directory / "predictions.jsonl"
    records = []
    if predictions_path.exists():
        with predictions_path.open("rb+") as stream:
            while line := stream.readline():
                if not line.endswith(b"\n"):
                    stream.seek(-len(line), 1)
                    stream.truncate()
                    break
                records.append(json.loads(line))
    if len(records) > len(samples):
        raise ValueError("Invalid persisted prediction count")
    for row, sample in zip(records, samples):
        if (row["sha256"], row["expected"]) != (sample["sha256"], sample["label"]):
            raise ValueError("Persisted predictions differ from benchmark manifest")
    previous_seconds = sum(row["wall_seconds"] for row in records)
    if (directory / "report.json").exists():
        previous_seconds = max(previous_seconds, json.loads(
            (directory / "report.json").read_text())["timing"]["wall_seconds"])
    started = time.monotonic()
    consecutive_errors = 0
    fd = os.open(predictions_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "a") as output:
        for sample in samples[len(records):]:
            raw = Path(sample["effective_path"]).read_bytes()
            user_content = prompts.render_email(raw, max_chars=parameters["max_chars"])
            request = {
                "model": parameters["model"], "stream": False, "format": "json",
                "keep_alive": "30m",
                "messages": [
                    {"role": "system", "content": prompts.SPAM_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                "options": {name: parameters[name] for name in
                            ("temperature", "seed", "num_ctx", "num_predict")},
            }
            before = time.monotonic()
            result = {
                "account": sample["account"], "sha256": sample["sha256"],
                "effective_sha256": sample["effective_sha256"],
                "expected": sample["label"], "predicted": "error",
                "rendered_characters": len(user_content),
            }
            for attempt in range(3):
                try:
                    response = local_json(base_url, "/api/chat", request)
                    for name in ("total_duration", "load_duration", "prompt_eval_count",
                                 "prompt_eval_cached_count", "prompt_eval_duration",
                                 "eval_count", "eval_duration"):
                        if name in response:
                            result[name] = response[name]
                    result["done_reason"] = response.get("done_reason")
                    try:
                        prediction = json.loads(response["message"]["content"])
                        label = prediction.get("label") if isinstance(prediction, dict) else None
                        result["predicted"] = label if label in ("ham", "spam") else "invalid"
                    except (ValueError, KeyError, TypeError):
                        result["predicted"] = "invalid"
                    result["attempts"] = attempt + 1
                    result.pop("error_type", None)
                    break
                except Exception as error:
                    result["error_type"] = type(error).__name__
                    result["attempts"] = attempt + 1
                    if attempt < 2:
                        time.sleep(2 ** attempt)
            result["wall_seconds"] = time.monotonic() - before
            output.write(json.dumps(result, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
            records.append(result)
            consecutive_errors = consecutive_errors + 1 if result["predicted"] == "error" else 0
            if len(records) % 25 == 0 or len(records) == len(samples):
                report = summarize(records, previous_seconds + time.monotonic() - started,
                                   input_rate=input_rate, output_rate=output_rate)
                report.update(run_id=run_id, parameters=parameters,
                              complete=len(records) == len(samples),
                              report_path=str(directory / "report.json"))
                atomic_json(directory / "report.json", report)
                event("evaluation_progress", completed=len(records), total=len(samples),
                      prompt_tokens=report["tokens"]["prompt_eval_count"],
                      generated_tokens=report["tokens"]["eval_count"])
            if consecutive_errors >= 3:
                event("evaluation_stopped", reason="consecutive_request_failures",
                      completed=len(records), total=len(samples))
                break
    report = summarize(records, previous_seconds + time.monotonic() - started,
                       input_rate=input_rate, output_rate=output_rate)
    report.update(run_id=run_id, parameters=parameters, complete=len(records) == len(samples),
                  report_path=str(directory / "report.json"))
    atomic_json(directory / "report.json", report)
    return report


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--model", default="llama3:latest")
    parser.add_argument("--per-label", type=int, default=1000)
    parser.add_argument("--input-usd-per-million", type=float)
    parser.add_argument("--output-usd-per-million", type=float)
    args = parser.parse_args(argv)
    try:
        environment = read_environment()
        result = evaluate(
            args.manifest,
            args.data_root if args.data_root is not None else environment.get(
                "HAMLET_DATA_ROOT", "~/hamlet-data"),
            model=args.model, per_label=args.per_label,
            input_rate=args.input_usd_per_million, output_rate=args.output_usd_per_million)
    except Exception as error:
        event("evaluation_failed", **failure_fields(error))
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

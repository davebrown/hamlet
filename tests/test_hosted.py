import hashlib
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

from hamlet import hosted


class SafeError(Exception):
    def __init__(self, *, status_code=None, retryable=False, ambiguous=True, result=None):
        self.status_code = status_code
        self.retryable = retryable
        self.ambiguous = ambiguous
        self.result = result
        super().__init__("private exception text must not persist")


class HostedTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / f".hosted-test-{uuid4().hex}"
        self.root.mkdir(mode=0o700)
        self.addCleanup(shutil.rmtree, self.root)
        self.samples = []
        for label in ("ham", "spam"):
            raw = f"Subject: synthetic {label}\r\n\r\nprivate synthetic mail {label}".encode()
            path = self.root / f"{label}.eml"
            path.write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            self.samples.append({
                "sha256": digest, "source_sha256": digest, "effective_sha256": digest,
                "source_path": str(path), "effective_path": str(path),
                "content_identity": digest, "partition": "test", "label": label,
            })
        self.log = self.root / "evaluations.md"
        self.original_log = (
            "# Existing evaluation log\n\n"
            "| Run timestamp (start, UTC) | Type | Model | Sample | Duration | "
            "Average per email | In tokens | Out tokens | API fees (USD) | Recall % | FP % | TP | FP | "
            "TN | FN | Invalid | Run ID |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
            "\n## Existing notes\n\nPreserve these notes.\n")
        self.log.write_text(self.original_log)
        self.specs = {"first": {"settings": {"temperature": 0}, "prices": {"input": 1}},
                      "second": {"settings": {"temperature": 0}, "prices": {"input": 2}}}
        self.manifest = self.root / "benchmark.json"
        self.manifest.write_text(json.dumps({"records": self.samples}))
        self.result = {
            "predicted": "spam", "input_tokens": 100, "output_tokens": 10,
            "cached_input_tokens": 5, "reasoning_tokens": 3,
            "estimated_cost_usd": .02, "usage_complete": True,
            "returned_model": "safe-model-snapshot", "stop_reason": "stop",
        }
        self.patches = [
            patch.object(hosted, "MODEL_SPECS", self.specs),
            patch.object(hosted, "_validate_manifest", return_value=(self.samples, "frozen")),
            patch.object(hosted, "ProviderError", SafeError),
        ]
        for context in self.patches:
            context.start()
            self.addCleanup(context.stop)

    def run_models(self, models=None):
        return hosted.evaluate(self.manifest, self.root, models=models,
                               environment={"TEST_KEY": "never persist me"},
                               evaluation_log=self.log)

    def test_progress_cost_confusion_privacy_and_resume_idempotent_log(self):
        def inference(model, system, user, environment):
            progress = json.loads((self.root / "runs/hosted-progress.json").read_text())
            self.assertEqual(set(progress["models"]), {"first", "second"})
            self.assertEqual(progress["model_concurrency"], 1)
            self.assertIn("private synthetic mail", user)
            self.assertEqual(system, hosted.prompts.SPAM_SYSTEM_PROMPT)
            return self.result

        with patch.object(hosted, "classify", side_effect=inference) as classify:
            report = self.run_models()
            self.assertTrue(report["complete"])
            self.assertEqual(classify.call_count, 4)
            first = report["reports"]["first"]
            self.assertEqual(first["tokens"]["input_tokens"], 200)
            self.assertEqual(first["tokens"]["reasoning_tokens"], 6)
            self.assertEqual(first["tokens"]["cached_input_tokens"], 10)
            self.assertAlmostEqual(first["cost"]["estimated_cost_usd"], .04)
            self.assertEqual(first["confusion"], {"ham_as_spam": 1, "spam_as_spam": 1})
            self.assertEqual(first["spam_recall"], 1)
            self.assertEqual(first["ham_false_positive_rate"], 1)
            self.assertIn("mean_request_seconds", first["timing"])
            self.assertIn("wall_emails_per_second", first["timing"])
            self.assertTrue(first["started_at"])
            self.assertTrue(first["ended_at"])
            again = self.run_models()
            self.assertTrue(again["complete"])
            self.assertEqual(classify.call_count, 4)
        content = self.log.read_text()
        self.assertIn("Preserve these notes.", content)
        self.assertEqual(content.count("| hosted smoke test |"), 2)
        for line in content.splitlines():
            if "| hosted smoke test |" in line:
                self.assertEqual(len(line.split("|")) - 2, 17)
        for path in (self.root / "runs").rglob("*.json"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            text = path.read_text()
            self.assertNotIn("private synthetic mail", text)
            self.assertNotIn("never persist me", text)
            self.assertNotIn("private exception text", text)

    def test_preflight_invalid_missing_usage_and_truncation_stop_entire_job(self):
        for change, expected_reason in (
                ({"predicted": "invalid"}, "invalid_preflight"),
                ({"usage_complete": False, "output_tokens": None}, "missing_usage"),
                ({"stop_reason": "max_tokens"}, "truncated_response")):
            with self.subTest(change=change):
                runs = self.root / "runs"
                if runs.exists():
                    shutil.rmtree(runs)
                with patch.object(hosted, "classify", return_value={**self.result, **change}) as classify:
                    result = self.run_models()
                    self.assertFalse(result["complete"])
                    self.assertEqual(classify.call_count, 1)
                    self.assertEqual(result["reports"]["first"]["blocked_reason"], expected_reason)
                    self.assertEqual(result["reports"]["first"]["tokens"]["input_tokens"], 100)
                    self.assertEqual(result["models"]["second"]["completed"], 0)
                    self.run_models()
                    self.assertEqual(classify.call_count, 1)
                self.assertEqual(self.log.read_text(), self.original_log)

    def test_ambiguous_error_never_retries_or_reissues_on_resume(self):
        with patch.object(hosted, "classify", side_effect=SafeError()) as classify:
            result = self.run_models()
            self.assertFalse(result["complete"])
            self.assertEqual(result["reports"]["first"]["blocked_reason"],
                             "ambiguous_provider_failure")
            self.assertEqual(classify.call_count, 1)
            self.assertEqual(len(list((self.root / "runs").rglob("in-flight.json"))), 1)
            self.run_models()
            self.assertEqual(classify.call_count, 1)
        self.assertEqual(self.log.read_text(), self.original_log)

    def test_received_usage_is_preserved_on_provider_error(self):
        with patch.object(hosted, "classify", side_effect=SafeError(result=self.result)):
            result = self.run_models()
        first = result["reports"]["first"]
        self.assertEqual(first["tokens"]["input_tokens"], 100)
        self.assertEqual(first["cost"]["estimated_cost_usd"], .02)
        self.assertEqual(first["invalid_or_failed"], 1)
        with patch.object(hosted, "classify") as classify:
            self.assertFalse(self.run_models()["complete"])
            classify.assert_not_called()

    def test_unavailable_reasoning_breakdown_does_not_block_known_billing_usage(self):
        response = {**self.result, "reasoning_tokens": None, "uncached_input_tokens": 90,
                    "cache_write_input_tokens": 5, "cache_write_short_tokens": 5,
                    "cache_write_long_tokens": 0}
        with patch.object(hosted, "classify", return_value=response):
            result = self.run_models(models=["first"])
        self.assertTrue(result["complete"])
        first = result["reports"]["first"]
        self.assertTrue(first["cost"]["usage_complete"])
        self.assertIsNone(first["tokens"]["reasoning_tokens"])
        self.assertEqual(first["tokens"]["responses_missing_counter"]["reasoning_tokens"], 2)
        self.assertEqual(first["tokens"]["cache_write_input_tokens"], 10)

    def test_crash_in_flight_blocks_without_duplicate_paid_request(self):
        with patch.object(hosted, "classify", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_models()
        with patch.object(hosted, "classify") as classify:
            result = self.run_models()
            self.assertFalse(result["complete"])
            self.assertEqual(result["reports"]["first"]["blocked_reason"], "uncertain_in_flight")
            self.assertFalse(result["reports"]["first"]["cost"]["usage_complete"])
            classify.assert_not_called()

    def test_durable_response_before_marker_cleanup_resumes_without_duplicate(self):
        original_unlink = Path.unlink
        crashed = False

        def crash_after_row(path, *args, **kwargs):
            nonlocal crashed
            if path.name == "in-flight.json" and not crashed:
                crashed = True
                raise KeyboardInterrupt
            return original_unlink(path, *args, **kwargs)

        with patch.object(hosted, "classify", return_value=self.result) as classify:
            with patch.object(Path, "unlink", crash_after_row):
                with self.assertRaises(KeyboardInterrupt):
                    self.run_models(models=["first"])
            self.assertEqual(classify.call_count, 1)
            result = self.run_models(models=["first"])
            self.assertTrue(result["complete"])
            self.assertEqual(classify.call_count, 2)

    def test_safe_429_is_bounded_and_invalid_response_is_not_retried(self):
        responses = [SafeError(status_code=429, retryable=True, ambiguous=False),
                     self.result, {**self.result, "predicted": "invalid"}]
        with patch.object(hosted, "classify", side_effect=responses) as classify:
            with patch.object(hosted.time, "sleep") as sleep:
                result = self.run_models(models=["first"])
        self.assertTrue(result["complete"])
        self.assertEqual(result["reports"]["first"]["invalid_or_failed"], 1)
        self.assertEqual(classify.call_count, 3)
        sleep.assert_called_once_with(1)

    def test_repeated_safe_rate_limits_stop_after_three_attempts(self):
        error = SafeError(status_code=429, retryable=True, ambiguous=False)
        with patch.object(hosted, "classify", side_effect=error) as classify:
            with patch.object(hosted.time, "sleep") as sleep:
                result = self.run_models()
        self.assertFalse(result["complete"])
        self.assertEqual(classify.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])
        self.assertEqual(result["models"]["second"]["completed"], 0)

    def test_auth_or_quota_rejection_and_log_misconfiguration_make_no_extra_calls(self):
        for status in (401, 403, 429):
            with self.subTest(status=status):
                runs = self.root / "runs"
                if runs.exists():
                    shutil.rmtree(runs)
                with patch.object(hosted, "classify",
                                  side_effect=SafeError(status_code=status, ambiguous=False)) as classify:
                    result = self.run_models()
                self.assertFalse(result["complete"])
                self.assertEqual(classify.call_count, 1)
        self.log.write_text("Not an evaluation table")
        with patch.object(hosted, "classify") as classify:
            with self.assertRaisesRegex(ValueError, "evaluation table"):
                self.run_models()
            classify.assert_not_called()

    def test_log_formats_duration_fees_and_rates_preserving_run_id(self):
        with patch.object(hosted, "classify", return_value=self.result):
            report = self.run_models(models=["first"])["reports"]["first"]
        self.log.write_text(self.original_log)
        report["timing"]["wall_seconds"] = 7190
        report["cost"]["estimated_cost_usd"] = 1.26614625
        report["spam_recall"] = .745
        report["ham_false_positive_rate"] = .02
        report["invalid_or_failed"] = 42
        hosted._append_log(self.log, report)
        hosted._append_log(self.log, report)
        lines = [line for line in self.log.read_text().splitlines()
                 if "| hosted smoke test |" in line]
        self.assertEqual(len(lines), 1)
        columns = [column.strip() for column in lines[0].strip("|").split("|")]
        self.assertEqual(len(columns), 17)
        self.assertEqual(columns[2], "`first`")
        self.assertEqual(columns[4], "2 hr 0 min")
        self.assertEqual(columns[8:11], ["$1.27 (estimated)", "74.5%", "2.0%"])
        self.assertEqual(columns[-2], "42")
        self.assertEqual(columns[-1], f'`{report["run_id"]}`')

    def test_prefix_tampering_is_rejected_before_provider_calls(self):
        with patch.object(hosted, "classify", return_value=self.result):
            self.run_models(models=["first"])
        row_path = next((self.root / "runs").rglob("row-0000.json"))
        row = json.loads(row_path.read_text())
        row["sha256"] = "wrong"
        hosted._atomic(row_path, row)
        with patch.object(hosted, "classify") as classify:
            with self.assertRaisesRegex(ValueError, "differ from benchmark"):
                self.run_models(models=["first"])
            classify.assert_not_called()

    def test_global_writer_lock_rejects_duplicate_orchestration(self):
        runs = hosted.private_directory(self.root / "runs")
        with hosted._lock(runs / "hosted-writer.lock"):
            with patch.object(hosted, "classify") as classify:
                with self.assertRaisesRegex(ValueError, "already active"):
                    self.run_models()
                classify.assert_not_called()

    def test_resilient_retries_uncertain_request_and_preserves_unknown_billing(self):
        error = SafeError(retryable=True)
        with patch.object(hosted, "classify", side_effect=[self.result, error, self.result]) as classify:
            with patch.object(hosted.time, "sleep") as sleep:
                result = hosted.evaluate(
                    self.manifest, self.root, models=["first"],
                    evaluation_log=self.log, resilient=True)
        self.assertTrue(result["complete"])
        self.assertEqual(classify.call_count, 3)
        sleep.assert_called_once_with(2)
        report = result["reports"]["first"]
        self.assertEqual(report["completed"], 2)
        self.assertEqual(report["tokens"]["input_tokens"], 200)
        self.assertEqual(report["cost"]["unknown_usage_attempts"], 1)
        self.assertFalse(report["cost"]["usage_complete"])
        self.assertIn("plus unknown attempt charges", self.log.read_text())
        self.assertEqual(len(list((self.root / "runs").rglob("attempt-*.json"))), 1)
        with patch.object(hosted, "classify") as classify:
            again = hosted.evaluate(self.manifest, self.root, models=["first"],
                                    evaluation_log=self.log, resilient=True)
        classify.assert_not_called()
        self.assertEqual(again["reports"]["first"]["cost"]["unknown_usage_attempts"], 1)
        self.assertEqual(self.log.read_text().count("| hosted smoke test |"), 1)

    def test_resilient_resumes_legacy_prefix_across_runner_revision(self):
        with patch.object(hosted, "classify", side_effect=[self.result, KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):
                self.run_models(models=["first"])
        row_path = next((self.root / "runs").rglob("row-0000.json"))
        original_row = row_path.read_bytes()
        old_directory = row_path.parent
        real_hash = hosted._hash_file
        def revision(path):
            return "new-runner-revision" if str(path) == hosted.__file__ else real_hash(path)
        with patch.object(hosted, "_hash_file", side_effect=revision):
            with patch.object(hosted, "classify", return_value=self.result) as classify:
                result = hosted.evaluate(
                    self.manifest, self.root, models=["first"],
                    evaluation_log=self.log, resilient=True)
        self.assertTrue(result["complete"])
        self.assertEqual(classify.call_count, 1)
        self.assertEqual(row_path.read_bytes(), original_row)
        self.assertEqual(len(list((self.root / "runs").glob("hosted-*/parameters.json"))), 1)
        self.assertEqual(result["reports"]["first"]["cost"]["unknown_usage_attempts"], 1)
        attempt = json.loads(next(old_directory.glob("attempt-*.json")).read_text())
        self.assertEqual(attempt["request"]["index"], 1)
        self.assertIn("prior_report", attempt)

    def test_resilient_bounded_retries_continue_other_models_and_save_usage(self):
        error = SafeError(status_code=503, retryable=True, result=self.result)
        responses = [error] * 5 + [self.result, self.result]
        with patch.object(hosted, "classify", side_effect=responses) as classify:
            with patch.object(hosted.time, "sleep") as sleep:
                result = hosted.evaluate(
                    self.manifest, self.root, evaluation_log=self.log, resilient=True)
        self.assertFalse(result["complete"])
        self.assertEqual(classify.call_count, 7)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4, 8, 16])
        first = result["reports"]["first"]
        self.assertEqual(first["completed"], 0)
        self.assertEqual(first["tokens"]["input_tokens"], 500)
        self.assertAlmostEqual(first["cost"]["estimated_cost_usd"], .10)
        self.assertEqual(first["cost"]["unknown_usage_attempts"], 0)
        self.assertEqual(first["failed_request_attempts"], 5)
        self.assertTrue(result["reports"]["second"]["complete"])

    def test_resilient_auth_failure_does_not_retry_but_other_models_continue(self):
        with patch.object(hosted, "classify", side_effect=[
                SafeError(status_code=401, ambiguous=False), self.result, self.result]) as classify:
            with patch.object(hosted.time, "sleep") as sleep:
                result = hosted.evaluate(
                    self.manifest, self.root, evaluation_log=self.log, resilient=True)
        self.assertEqual(classify.call_count, 3)
        sleep.assert_not_called()
        self.assertFalse(result["reports"]["first"]["complete"])
        self.assertTrue(result["reports"]["first"]["cost"]["usage_complete"])
        self.assertTrue(result["reports"]["second"]["complete"])

    def test_recovery_crash_does_not_duplicate_unknown_attempt_accounting(self):
        with patch.object(hosted, "classify", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_models(models=["first"])
        real_unlink = Path.unlink
        def crash(path, *args, **kwargs):
            if path.name == "in-flight.json":
                raise KeyboardInterrupt
            return real_unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", crash):
            with self.assertRaises(KeyboardInterrupt):
                hosted.evaluate(self.manifest, self.root, models=["first"],
                                evaluation_log=self.log, resilient=True)
        with patch.object(hosted, "classify", return_value=self.result):
            result = hosted.evaluate(self.manifest, self.root, models=["first"],
                                     evaluation_log=self.log, resilient=True)
        self.assertTrue(result["complete"])
        self.assertEqual(result["reports"]["first"]["cost"]["unknown_usage_attempts"], 1)
        self.assertEqual(len(list((self.root / "runs").rglob("attempt-*.json"))), 1)


class HostedManifestTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / f".hosted-manifest-test-{uuid4().hex}"
        self.root.mkdir(mode=0o700)
        self.addCleanup(shutil.rmtree, self.root)
        self.samples = []
        for index in range(2000):
            raw = f"synthetic mail {index}".encode()
            path = self.root / f"{index}.eml"
            path.write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            self.samples.append({
                "label": "ham" if index < 1000 else "spam", "partition": "test",
                "sha256": digest, "effective_sha256": digest, "source_path": str(path),
                "effective_path": str(path), "content_identity": digest,
            })
        self.manifest = self.root / "benchmark.json"

    def validate(self):
        self.manifest.write_text(json.dumps({"records": self.samples}))
        return hosted._validate_manifest(self.manifest, self.root)

    def test_frozen_balanced_integrity_and_rejected_mutations(self):
        samples, digest = self.validate()
        self.assertEqual(len(samples), 2000)
        self.assertEqual(len(digest), 64)
        for field, value, message in (
                ("partition", "dev", "test partition"),
                ("content_identity", self.samples[1]["content_identity"], "Duplicate"),
                ("source_path", str(self.root.parent / "outside.eml"), "outside"),
                ("sha256", "0" * 64, "integrity"),
                ("effective_sha256", "0" * 64, "integrity"),
                ("label", "spam", "exactly 1000")):
            with self.subTest(field=field):
                previous = self.samples[0][field]
                self.samples[0][field] = value
                with self.assertRaisesRegex(ValueError, message):
                    self.validate()
                self.samples[0][field] = previous


if __name__ == "__main__":
    unittest.main()

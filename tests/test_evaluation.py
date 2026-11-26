import json
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hamlet.evaluation import _run, evaluate, local_json, summarize, upper_binomial


class EvaluationTests(unittest.TestCase):
    def test_exact_zero_failure_bound_and_known_single_trial(self):
        self.assertAlmostEqual(upper_binomial(0, 1000), 0.002991249545, places=10)
        self.assertAlmostEqual(upper_binomial(1, 2), 0.97467943448, places=10)
        self.assertIsNone(upper_binomial(0, 0))

    def test_metrics_and_hypothetical_price_do_not_claim_local_charge(self):
        records = [
            {"expected": "ham", "predicted": "spam", "wall_seconds": 2,
             "prompt_eval_count": 100, "eval_count": 10},
            {"expected": "spam", "predicted": "spam", "wall_seconds": 3,
             "prompt_eval_count": 200, "eval_count": 20},
            {"expected": "ham", "predicted": "invalid", "wall_seconds": 1},
        ]
        result = summarize(records, 7, input_rate=1, output_rate=2)
        self.assertEqual(result["tokens"]["prompt_plus_generation"], 330)
        self.assertEqual(result["invalid_or_failed"], 1)
        self.assertEqual(result["spam_recall"], 1)
        self.assertEqual(result["timing"]["request_wall_seconds"], 6)
        self.assertEqual(result["cost"]["local_api_charge_usd"], 0)
        self.assertAlmostEqual(result["cost"]["hypothetical_api_usd"], .00036)

    def test_only_local_ollama_is_allowed(self):
        for endpoint in ("https://example.invalid", "http://example.invalid",
                         "http://localhost@evil.invalid", "http://localhost/path"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                local_json(endpoint, "/api/chat", {})

    def test_records_time_tokens_and_resumes_without_mail_or_output_leak(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "mail.eml"
            path.write_bytes(b"private mail body")
            sample = {"account": "a", "sha256": "source", "effective_sha256": "effective",
                      "label": "ham", "effective_path": str(path)}
            parameters = {"model": "basic", "max_chars": 4000, "temperature": 0, "seed": 42,
                          "num_ctx": 2048, "num_predict": 32}
            prompts = SimpleNamespace(SPAM_SYSTEM_PROMPT="system",
                                      render_email=lambda raw, **kw: raw.decode())
            response = {"message": {"content": '{"label":"ham"}'}, "prompt_eval_count": 123,
                        "eval_count": 7, "total_duration": 1_000_000_000}
            with patch("hamlet.evaluation.local_json", return_value=response) as inference:
                report = _run([sample], parameters, root, "run", prompts, "local", None, None)
                self.assertEqual(report["tokens"]["prompt_eval_count"], 123)
                self.assertEqual(report["tokens"]["eval_count"], 7)
                self.assertEqual(report["confusion"], {"ham_as_ham": 1})
                self.assertEqual(inference.call_count, 1)
                with (root / "predictions.jsonl").open("ab") as interrupted:
                    interrupted.write(b'{"sha256":"incomplete')
                again = _run([sample], parameters, root, "run", prompts, "local", None, None)
                self.assertEqual(inference.call_count, 1)
                self.assertEqual(again["completed"], 1)
                self.assertGreaterEqual(again["timing"]["wall_seconds"],
                                        report["timing"]["wall_seconds"])
            self.assertNotIn("private mail body", (root / "predictions.jsonl").read_text())
            self.assertEqual((root / "predictions.jsonl").stat().st_mode & 0o777, 0o600)
            record = json.loads((root / "predictions.jsonl").read_text())
            self.assertNotIn("response", record)

    def test_invalid_model_output_is_counted_not_coerced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "mail.eml"
            path.write_bytes(b"synthetic")
            parameters = {"model": "basic", "max_chars": 20, "temperature": 0, "seed": 42,
                          "num_ctx": 2048, "num_predict": 32}
            prompts = SimpleNamespace(SPAM_SYSTEM_PROMPT="system",
                                      render_email=lambda raw, **kw: "synthetic")
            samples = [{"account": "a", "sha256": "one", "effective_sha256": "one",
                        "label": "spam", "effective_path": str(path)}]
            with patch("hamlet.evaluation.local_json",
                       return_value={"message": {"content": "not JSON"}}):
                report = _run(samples, parameters, root, "run", prompts, "local", None, None)
            self.assertEqual(report["invalid_or_failed"], 1)
            self.assertEqual(report["spam_recall"], 0)

    def test_manifest_validation_and_end_to_end_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            samples = []
            for label in ("ham", "spam"):
                raw = f"Subject: {label} synthetic\r\n\r\nSynthetic {label}".encode()
                path = root / f"{label}.eml"
                path.write_bytes(raw)
                digest = hashlib.sha256(raw).hexdigest()
                samples.append({"account": "a", "sha256": digest, "effective_sha256": digest,
                                "label": label, "partition": "test", "effective_path": str(path)})
            manifest = root / "benchmark.json"
            manifest.write_text(json.dumps({"records": samples}))
            responses = [
                {"models": [{"name": "basic", "digest": "model-digest"}]},
                {"message": {"content": '{"label":"ham"}'}, "prompt_eval_count": 10, "eval_count": 7},
                {"message": {"content": '{"label":"spam"}'}, "prompt_eval_count": 20, "eval_count": 7},
            ]
            with patch("hamlet.evaluation.local_json", side_effect=responses):
                result = evaluate(manifest, root, model="basic", per_label=1)
            self.assertTrue(result["complete"])
            self.assertEqual(result["tokens"]["prompt_plus_generation"], 44)
            self.assertTrue(Path(result["report_path"]).exists())
            samples[0]["partition"] = "dev"
            manifest.write_text(json.dumps({"records": samples}))
            with self.assertRaisesRegex(ValueError, "test partition"):
                evaluate(manifest, root, model="basic", per_label=1)

    def test_repeated_request_failures_stop_incomplete_without_exception_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "mail.eml"
            path.write_bytes(b"synthetic")
            parameters = {"model": "basic", "max_chars": 20, "temperature": 0, "seed": 42,
                          "num_ctx": 2048, "num_predict": 32}
            prompts = SimpleNamespace(SPAM_SYSTEM_PROMPT="system",
                                      render_email=lambda raw, **kw: "synthetic")
            samples = [{"account": "a", "sha256": str(index), "effective_sha256": str(index),
                        "label": "spam", "effective_path": str(path)} for index in range(5)]
            with patch("hamlet.evaluation.local_json", side_effect=OSError("private payload")):
                with patch("hamlet.evaluation.time.sleep"):
                    report = _run(samples, parameters, root, "run", prompts, "local", None, None)
            self.assertFalse(report["complete"])
            self.assertEqual(report["completed"], 3)
            self.assertNotIn("private payload", (root / "predictions.jsonl").read_text())

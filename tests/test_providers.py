import io
import json
import ssl
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from hamlet.providers import MODEL_SPECS, ProviderError, _NoRedirect, classify


class ProviderTests(unittest.TestCase):
    def call(self, model="gpt-6.2-sol", payload=None, raw=None):
        if raw is None:
            raw = json.dumps(payload if payload is not None else self.openai()).encode()
        with patch("hamlet.providers.request.build_opener") as build:
            build.return_value.open.return_value.__enter__.return_value = io.BytesIO(raw)
            result = classify(model, "PRIVATE_SYSTEM", "PRIVATE_MAIL",
                              {"OPENAI_API_KEY": "PRIVATE_KEY", "ANTHROPIC_API_KEY": "PRIVATE_KEY"})
            req = build.return_value.open.call_args.args[0]
            handlers = build.call_args.args
            self.assertEqual(build.return_value.open.call_count, 1)
            self.assertEqual(build.return_value.open.call_args.kwargs["timeout"], 90)
        return result, req, handlers

    def openai(self, text='{"label":"ham"}'):
        return {"model": "gpt-6.2-sol", "status": "completed",
                "output": [{"type": "reasoning", "summary": [{"text": "PRIVATE_REASONING"}]},
                           {"type": "message", "role": "assistant", "status": "completed",
                            "content": [{"type": "output_text", "text": text}]}],
                "usage": {"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 400},
                          "output_tokens": 100, "output_tokens_details": {"reasoning_tokens": 90}}}

    def claude(self, text='{"label":"spam"}'):
        return {"model": "claude-opus-5-5", "stop_reason": "end_turn",
                "content": [{"type": "thinking", "thinking": "PRIVATE_REASONING"},
                            {"type": "text", "text": text}],
                "usage": {"input_tokens": 100, "output_tokens": 200,
                          "cache_read_input_tokens": 400,
                          "cache_creation_input_tokens": 1000,
                          "cache_creation": {"ephemeral_5m_input_tokens": 600,
                                             "ephemeral_1h_input_tokens": 400}}}

    def test_requests_for_all_models_are_fixed_private_and_settings_exact(self):
        self.assertEqual(len(MODEL_SPECS), 7)
        json.dumps(MODEL_SPECS)
        for model, spec in MODEL_SPECS.items():
            with self.subTest(model=model):
                payload = self.claude() if spec["provider"] == "anthropic" else self.openai()
                _, req, handlers = self.call(model, payload)
                body = json.loads(req.data)
                for key, value in spec["settings"].items():
                    self.assertEqual(body[key], value)
                self.assertEqual(body["model"], model)
                self.assertNotIn("temperature", body)
                self.assertNotIn("seed", body)
                self.assertNotIn("cache_control", json.dumps(body))
                self.assertEqual(handlers[0].proxies, {})
                self.assertIsInstance(handlers[1], _NoRedirect)
                self.assertEqual(handlers[2]._context.verify_mode, ssl.CERT_REQUIRED)
                self.assertTrue(handlers[2]._context.check_hostname)
                if spec["provider"] == "anthropic":
                    self.assertEqual(req.full_url, "https://api.anthropic.com/v1/messages")
                    self.assertEqual(req.get_header("Anthropic-version"), "2026-10-21")
                else:
                    self.assertEqual(req.full_url, "https://api.openai.com/v1/responses")
                    self.assertIs(body["store"], False)
                    if model != "gpt-5.4-mini":
                        self.assertEqual(body["prompt_cache_options"], {"mode": "explicit"})
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil"))

    def test_openai_cache_and_reasoning_are_not_double_billed(self):
        result, _, _ = self.call()
        self.assertEqual(result["predicted"], "ham")
        self.assertEqual(result["input_tokens"], 1000)
        self.assertEqual(result["reasoning_tokens"], 90)
        self.assertEqual(result["output_tokens"], 100)
        self.assertAlmostEqual(result["estimated_cost_usd"], (600 * 2 + 400 * .1 + 100 * 10) / 1e6)
        self.assertTrue(result["usage_complete"])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_openai_paid_cache_writes_are_disjoint_and_priced_by_lifetime(self):
        payload = self.openai()
        payload["usage"]["input_tokens_details"].update(
            cache_write_tokens=300, cache_write_tokens_details={"ttl_30m_tokens": 100,
                                                               "ttl_12h_tokens": 200})
        result, _, _ = self.call(payload=payload)
        self.assertEqual(result["uncached_input_tokens"], 300)
        self.assertAlmostEqual(result["estimated_cost_usd"],
                               (300 * 2 + 400 * .1 + 100 * 2.5 + 200 * 5 + 100 * 10) / 1e6)
        self.assertTrue(result["usage_complete"])

    def test_claude_input_excludes_cache_usage_and_output_includes_thinking(self):
        result, _, _ = self.call("claude-opus-5-5", self.claude())
        self.assertEqual(result["predicted"], "spam")
        self.assertEqual(result["input_tokens"], 1500)
        self.assertIsNone(result["reasoning_tokens"])
        self.assertAlmostEqual(result["estimated_cost_usd"],
                               (100 * 4 + 400 * .2 + 600 * 5 + 400 * 8 + 200 * 20) / 1e6)
        self.assertTrue(result["usage_complete"])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_invalid_or_truncated_output_keeps_paid_usage(self):
        for text in ('{"label":"ham","extra":"PRIVATE_MAIL"}', '```{"label":"ham"}```',
                     '{"label":"ham","label":"spam"}', '{"label":"HAM"}', "PRIVATE_MAIL"):
            result, _, _ = self.call(payload=self.openai(text))
            self.assertEqual(result["predicted"], "invalid")
            self.assertEqual(result["output_tokens"], 100)
            self.assertTrue(result["usage_complete"])
            self.assertNotIn("PRIVATE", json.dumps(result))
        for provider in ("openai", "anthropic"):
            payload = self.openai() if provider == "openai" else self.claude()
            payload["status" if provider == "openai" else "stop_reason"] = (
                "incomplete" if provider == "openai" else "max_tokens")
            result, _, _ = self.call("gpt-6.2-sol" if provider == "openai"
                                     else "claude-opus-5-5", payload)
            self.assertEqual(result["predicted"], "invalid")
            self.assertTrue(result["truncated"])
            self.assertTrue(result["usage_complete"])

    def test_missing_and_malformed_usage_preserves_known_counts(self):
        for field in ("input_tokens", "output_tokens", "input_tokens_details", "output_tokens_details"):
            payload = self.openai()
            del payload["usage"][field]
            result, _, _ = self.call(payload=payload)
            self.assertFalse(result["usage_complete"])
            self.assertEqual(result["predicted"], "invalid")
            self.assertEqual(result["output_tokens"], None if field == "output_tokens" else 100)
        for value in (True, -1, 1.5, "PRIVATE_MAIL", None):
            payload = self.openai()
            payload["usage"]["input_tokens"] = value
            result, _, _ = self.call(payload=payload)
            self.assertFalse(result["usage_complete"])
        payload = self.claude()
        del payload["usage"]["cache_creation"]
        result, _, _ = self.call("claude-opus-5-5", payload)
        self.assertFalse(result["usage_complete"])
        self.assertEqual(result["cache_write_input_tokens"], 1000)

    def test_missing_or_unexpected_output_and_metadata_are_not_trusted(self):
        payload = self.openai()
        payload.update(model="PRIVATE_MAIL", status="PRIVATE_MAIL", output=None)
        result, _, _ = self.call(payload=payload)
        self.assertEqual(result["predicted"], "invalid")
        self.assertIsNone(result["returned_model"])
        self.assertEqual(result["stop_reason"], "unknown")
        self.assertNotIn("PRIVATE", json.dumps(result))
        for content in ([], [{"type": "refusal", "refusal": "PRIVATE_MAIL"}]):
            payload = self.openai()
            payload["output"][1]["content"] = content
            result, _, _ = self.call(payload=payload)
            self.assertEqual(result["predicted"], "invalid")

    def test_http_errors_are_safe_single_attempt_and_preserve_known_usage(self):
        for status in (302, 400, 401, 403, 408, 429, 500, 529):
            with self.subTest(status=status), patch("hamlet.providers.request.build_opener") as build:
                build.return_value.open.side_effect = HTTPError(
                    "https://PRIVATE_KEY", status, "PRIVATE_MAIL", {"PRIVATE": "MAIL"},
                    io.BytesIO(json.dumps(self.openai()).encode()))
                with self.assertRaises(ProviderError) as caught:
                    classify("gpt-6.2-sol", "PRIVATE", "PRIVATE", {"OPENAI_API_KEY": "PRIVATE_KEY"})
                exc = caught.exception
                self.assertEqual(exc.status_code, status)
                self.assertEqual(build.return_value.open.call_count, 1)
                self.assertNotIn("PRIVATE", str(exc))
                self.assertNotIn("PRIVATE", json.dumps(exc.__dict__))
                self.assertEqual(exc.result["output_tokens"], 100)
                self.assertEqual(exc.result["predicted"], "invalid")

    def test_transport_and_invalid_json_fail_ambiguously_without_provider_body(self):
        for raw in (b"PRIVATE_MAIL", b"[]", b'{"a":1,"a":2}', b'{"a":NaN}'):
            with self.assertRaises(ProviderError) as caught:
                self.call(raw=raw)
            self.assertTrue(caught.exception.ambiguous)
            self.assertNotIn("PRIVATE", str(caught.exception))
        with patch("hamlet.providers.request.build_opener") as build:
            build.return_value.open.side_effect = URLError("PRIVATE_MAIL")
            with self.assertRaises(ProviderError) as caught:
                classify("gpt-6-luna", "x", "y", {"OPENAI_API_KEY": "PRIVATE_KEY"})
            self.assertTrue(caught.exception.ambiguous)
            self.assertEqual(str(caught.exception), "transport_error")

    def test_preflight_validation_never_calls_network(self):
        with patch("hamlet.providers.request.build_opener") as build:
            for model, environment in (("bad", {}), ("gpt-6-luna", {}),
                                       ("gpt-6-luna", {"OPENAI_API_KEY": "key\nbad"})):
                with self.assertRaises(ProviderError) as caught:
                    classify(model, "x", "y", environment)
                self.assertFalse(caught.exception.ambiguous)
            build.assert_not_called()

    def test_timeout_and_http_retry_diagnostics_are_safe(self):
        with patch("hamlet.providers.request.build_opener") as build:
            build.return_value.open.side_effect = URLError(TimeoutError("PRIVATE_MAIL"))
            with self.assertRaises(ProviderError) as caught:
                classify("gpt-6-luna", "x", "y", {"OPENAI_API_KEY": "PRIVATE_KEY"})
        self.assertEqual(caught.exception.cause, "timeout")
        self.assertTrue(caught.exception.retryable)
        self.assertNotIn("PRIVATE", json.dumps(caught.exception.__dict__))
        body = json.dumps({"error": {"code": "unsupported_parameter",
                                    "param": "prompt_cache_options",
                                    "message": "PRIVATE_MAIL"}}).encode()
        with patch("hamlet.providers.request.build_opener") as build:
            build.return_value.open.side_effect = HTTPError(
                "https://PRIVATE_KEY", 400, "PRIVATE_MAIL", {"Retry-After": "30"}, io.BytesIO(body))
            with self.assertRaises(ProviderError) as caught:
                classify("gpt-6-luna", "x", "y", {"OPENAI_API_KEY": "PRIVATE_KEY"})
        self.assertEqual(caught.exception.error_code, "unsupported_parameter")
        self.assertEqual(caught.exception.parameter, "prompt_cache_options")
        self.assertEqual(caught.exception.retry_after, 30)
        self.assertFalse(caught.exception.retryable)
        self.assertNotIn("PRIVATE", json.dumps(caught.exception.__dict__))

    def test_long_context_prices_use_total_input(self):
        payload = self.openai()
        payload["usage"]["input_tokens"] = 273000
        result, _, _ = self.call(payload=payload)
        self.assertAlmostEqual(result["estimated_cost_usd"],
                               ((272600 * 2 + 400 * .1) * 2 + 100 * 10 * 1.5) / 1e6)
        payload = self.claude()
        payload["usage"]["input_tokens"] = 100000
        result, _, _ = self.call("claude-haiku-5-5", payload)
        self.assertAlmostEqual(result["estimated_cost_usd"],
                               (100000 * .1 + 400 * .01 + 600 * .125 + 400 * .2 + 200 * .5) * 5 / 1e6)

    def test_http_errors_without_usage_never_expose_bodies_or_retry(self):
        for raw in (b"PRIVATE_MAIL", b'{"error":{"message":"PRIVATE_MAIL"}}'):
            with patch("hamlet.providers.request.build_opener") as build:
                build.return_value.open.side_effect = HTTPError(
                    "https://PRIVATE_KEY", 429, "PRIVATE_MAIL", {}, io.BytesIO(raw))
                with self.assertRaises(ProviderError) as caught:
                    classify("gpt-6-luna", "x", "y", {"OPENAI_API_KEY": "PRIVATE_KEY"})
                self.assertEqual(build.return_value.open.call_count, 1)
                self.assertFalse(caught.exception.retryable)
                self.assertFalse(caught.exception.ambiguous)
                self.assertIsNone(caught.exception.result)
                self.assertNotIn("PRIVATE", json.dumps(caught.exception.__dict__))

    def test_only_explicit_rate_limit_429_errors_are_retryable_not_quota(self):
        cases = [
            ({"type": "rate_limit_error"}, True),
            ({"code": "rate_limit_exceeded"}, True),
            ({"type": "tokens", "code": "rate_limit_exceeded"}, True),
            ({"type": "insufficient_quota"}, False),
            ({"code": "billing_hard_limit_reached"}, False),
            ({"code": "payment_required"}, False),
            ({"code": "credit_balance_too_low"}, False),
            ({"type": "rate_limit_error", "code": "insufficient_quota"}, False),
            ({"type": "unknown", "message": "rate_limit_exceeded PRIVATE_MAIL"}, False),
            ({}, False),
        ]
        for provider_error, expected in cases:
            with self.subTest(provider_error=provider_error), patch(
                    "hamlet.providers.request.build_opener") as build:
                body = json.dumps({"error": provider_error}).encode()
                build.return_value.open.side_effect = HTTPError(
                    "https://PRIVATE_KEY", 429, "PRIVATE_MAIL", {}, io.BytesIO(body))
                with self.assertRaises(ProviderError) as caught:
                    classify("gpt-6-luna", "x", "y", {"OPENAI_API_KEY": "PRIVATE_KEY"})
                self.assertEqual(caught.exception.retryable, expected)
                self.assertFalse(caught.exception.ambiguous)
                self.assertIsNone(caught.exception.result)
                self.assertEqual(build.return_value.open.call_count, 1)
                self.assertNotIn("PRIVATE", json.dumps(caught.exception.__dict__))

    def test_response_size_and_nonsensical_counts_are_bounded(self):
        with patch("hamlet.providers._MAX_RESPONSE_BYTES", 10):
            with self.assertRaisesRegex(ProviderError, "response_too_large") as caught:
                self.call(raw=b"x" * 11)
            self.assertTrue(caught.exception.ambiguous)
        payload = self.openai()
        payload["usage"]["input_tokens"] = 10**1000
        result, _, _ = self.call(payload=payload)
        self.assertFalse(result["usage_complete"])
        self.assertIsNone(result["input_tokens"])

    def test_unexpected_model_blocks_pricing_claim_and_dated_snapshot_is_supported(self):
        payload = self.openai()
        payload["model"] = "gpt-6.2-sol-2026-11-18"
        result, _, _ = self.call(payload=payload)
        self.assertEqual(result["returned_model"], payload["model"])
        self.assertTrue(result["usage_complete"])
        payload["model"] = "gpt-6.2-astra"
        result, _, _ = self.call(payload=payload)
        self.assertIsNone(result["returned_model"])
        self.assertEqual(result["predicted"], "invalid")
        self.assertFalse(result["usage_complete"])


if __name__ == "__main__":
    unittest.main()

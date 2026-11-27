"""Single-attempt hosted classifiers; only content-free results leave this module.

``input_tokens`` is total input, including cache reads/writes, for both APIs.
``output_tokens`` already includes reasoning: never add ``reasoning_tokens``.
Unknown counts are None, not zero. ``usage_complete`` means billing can be
estimated, not that the provider exposes a separate reasoning-token breakdown.
Prices are standard USD per million tokens, verified 2026-11-26.
"""

from copy import deepcopy
from collections.abc import Mapping
import http.client
import json
import re
import socket
import ssl
from urllib import error, request


_CLAUDE_DOCS = "https://platform.claude.com/docs/en/"
_OPENAI_DOCS = "https://developers.openai.com/api/docs/"
_ENDPOINTS = {
    "anthropic": "https://api.anthropic.com/v1/messages",
    "openai": "https://api.openai.com/v1/responses",
}
_TIMEOUT = 90
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def _spec(provider, input_price, cache_price, output_price, settings, source):
    prices = {"input": input_price, "cached_input": cache_price,
              "output": output_price, "unit": "USD_per_million_tokens"}
    if provider == "anthropic":
        prices.update(cache_write_5m=input_price * 1.25, cache_write_1h=input_price * 2)
    elif "prompt_cache_options" in settings:
        prices.update(cache_write_30m=input_price * 1.25, cache_write_12h=input_price * 2.5)
    return {"provider": provider, "settings": settings, "prices": prices,
            "api_version": "2026-10-21" if provider == "anthropic" else None,
            "sources": [source, (_CLAUDE_DOCS + "about-claude/pricing"
                                if provider == "anthropic" else _OPENAI_DOCS + "pricing")],
            "verified_date": "2026-11-26"}


MODEL_SPECS = {
    "claude-haiku-5-5": _spec("anthropic", .10, .01, .50,
        {"thinking": {"type": "disabled"}, "output_config": {"effort": "low"},
         "max_tokens": 128}, _CLAUDE_DOCS + "models/haiku-5-5/whats-new-haiku-5-5"),
    "claude-sonnet-5-5": _spec("anthropic", 2, .10, 10,
        {"thinking": {"type": "between_tools"}, "output_config": {"effort": "low"},
         "max_tokens": 128}, _CLAUDE_DOCS + "models/sonnet-5-5/whats-new-sonnet-5-5"),
    "claude-opus-5-5": _spec("anthropic", 4, .20, 20,
        {"thinking": {"type": "adaptive"}, "output_config": {"effort": "low"},
         "max_tokens": 4096}, _CLAUDE_DOCS + "models/opus-5-5/whats-new-opus-5-5"),
}
for _model, _input, _cache, _output, _effort in (
    ("gpt-6-luna", .10, .01, .50, "none"),
    ("gpt-5.4-mini", .75, .075, 4.5, "none"),
    ("gpt-6.1-terra", 1, .05, 5, "none"),
    ("gpt-6.2-sol", 2, .10, 10, "low"),
):
    _settings = {"reasoning": {"effort": _effort},
                 "max_output_tokens": 4096 if _effort == "low" else 128,
                 "store": False, "service_tier": "default"}
    if _model != "gpt-5.4-mini":
        # Explicit-only mode with no breakpoints disables paid cache writes.
        _settings["prompt_cache_options"] = {"mode": "explicit"}
    MODEL_SPECS[_model] = _spec("openai", _input, _cache, _output, _settings,
                              _OPENAI_DOCS + "models/" + _model)
MODEL_SPECS["claude-haiku-5-5"]["prices"].update(
    long_context_threshold=100_000, long_context_input_multiplier=5,
    long_context_output_multiplier=5)
for _model in ("gpt-6-luna", "gpt-6.1-terra", "gpt-6.2-sol"):
    MODEL_SPECS[_model]["prices"].update(
        long_context_threshold=272_000, long_context_input_multiplier=2,
        long_context_output_multiplier=1.5)


class ProviderError(Exception):
    """Safe error codes only; ambiguous failures must not be blindly retried."""

    def __init__(self, message, *, status_code=None, retryable=False,
                 ambiguous=False, result=None, cause=None, retry_after=None,
                 error_code=None, parameter=None):
        codes = {"unsupported_model", "missing_api_key", "invalid_request",
                 "transport_error", "http_error", "invalid_response",
                 "response_too_large", "redirect_rejected"}
        self.message = message if message in codes else "invalid_response"
        self.status_code = status_code if type(status_code) is int else None
        self.retryable = bool(retryable)
        self.ambiguous = bool(ambiguous)
        self.result = result
        self.cause = cause if cause in (
            "timeout", "dns", "tls", "connection", "protocol") else None
        self.retry_after = (min(retry_after, 600) if type(retry_after) is int
                            and retry_after >= 0 else None)
        self.error_code = error_code if error_code in (
            "rate_limit_error", "rate_limit_exceeded", "insufficient_quota",
            "invalid_request_error", "unsupported_parameter", "unsupported_value",
            "authentication_error", "permission_error", "overloaded_error",
            "billing_hard_limit_reached", "payment_required", "credit_balance_too_low") else None
        self.parameter = parameter if parameter in (
            "model", "max_tokens", "max_output_tokens", "thinking", "thinking.type",
            "output_config", "output_config.effort", "reasoning", "reasoning.effort",
            "store", "service_tier", "prompt_cache_options") else None
        super().__init__(self.message)


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _integer(value):
    return value if type(value) is int and 0 <= value <= 10**12 else None


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _json(raw):
    return json.loads(raw, object_pairs_hook=_strict_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))


def _label(text):
    try:
        parsed = _json(text)
    except (ValueError, TypeError, RecursionError):
        return "invalid"
    if (isinstance(parsed, dict) and set(parsed) == {"label"}
            and parsed["label"] in ("ham", "spam")):
        return parsed["label"]
    return "invalid"


def _usage(model, payload):
    spec = MODEL_SPECS[model]
    usage = _mapping(payload.get("usage"))
    incoming = _integer(usage.get("input_tokens"))
    outgoing = _integer(usage.get("output_tokens"))
    complete = incoming is not None and outgoing is not None
    anthropic = spec["provider"] == "anthropic"
    if anthropic:
        cached = _integer(usage.get("cache_read_input_tokens", 0))
        written = _integer(usage.get("cache_creation_input_tokens", 0))
        details = _mapping(usage.get("cache_creation"))
        short = _integer(details.get("ephemeral_5m_input_tokens", 0))
        long = _integer(details.get("ephemeral_1h_input_tokens", 0))
        # A nonzero write without lifetime details cannot be priced reliably.
        complete &= None not in (cached, written, short, long)
        complete &= short is not None and long is not None and written == short + long
        ordinary = incoming
        total = (incoming + cached + written
                 if None not in (incoming, cached, written) else None)
        reasoning = 0 if spec["settings"]["thinking"]["type"] != "adaptive" else None
    else:
        details = _mapping(usage.get("input_tokens_details"))
        cached = _integer(details.get("cached_tokens"))
        written = _integer(details.get("cache_write_tokens", 0))
        lifetimes = _mapping(details.get("cache_write_tokens_details"))
        short = _integer(lifetimes.get("ttl_30m_tokens", 0))
        long = _integer(lifetimes.get("ttl_12h_tokens", 0))
        reasoning = _integer(_mapping(usage.get("output_tokens_details")).get("reasoning_tokens"))
        complete &= None not in (cached, written, short, long, reasoning)
        complete &= short is not None and long is not None and written == short + long
        complete &= reasoning is not None and outgoing is not None and reasoning <= outgoing
        total = incoming
        ordinary = (incoming - cached - written
                    if None not in (incoming, cached, written) else None)
        complete &= ordinary is not None and ordinary >= 0
        if written and "cache_write_30m" not in spec["prices"]:
            complete = False
    prices = spec["prices"]
    long_context = total is not None and total > prices.get("long_context_threshold", float("inf"))
    input_multiplier = prices.get("long_context_input_multiplier", 1) if long_context else 1
    output_multiplier = prices.get("long_context_output_multiplier", 1) if long_context else 1
    # Preserve a lower-bound estimate from known components, never invent counts.
    cost = max(ordinary or 0, 0) * prices["input"] + (cached or 0) * prices["cached_input"]
    short_rate = prices.get("cache_write_5m" if anthropic else "cache_write_30m", 0)
    long_rate = prices.get("cache_write_1h" if anthropic else "cache_write_12h", 0)
    cost += (short or 0) * short_rate + (long or 0) * long_rate
    cost = (cost * input_multiplier + (outgoing or 0) * prices["output"] * output_multiplier) / 1e6
    return {"input_tokens": total, "uncached_input_tokens": ordinary,
            "output_tokens": outgoing, "cached_input_tokens": cached,
            "cache_write_input_tokens": written, "cache_write_short_tokens": short,
            "cache_write_long_tokens": long, "reasoning_tokens": reasoning,
            "usage_complete": bool(complete), "estimated_cost_usd": cost,
            "cost_is_lower_bound": not bool(complete)}


def _result(model, payload):
    spec = MODEL_SPECS[model]
    result = _usage(model, payload)
    returned = payload.get("model")
    # Model/stop metadata is untrusted too: do not persist arbitrary strings.
    returned = returned if (isinstance(returned, str)
        and re.fullmatch(re.escape(model) + r"(?:-\d{4}-?\d{2}-?\d{2})?", returned)) else None
    texts = []
    valid = True
    if spec["provider"] == "anthropic":
        reason = payload.get("stop_reason")
        reason = reason if reason in (
            "end_turn", "max_tokens", "stop_sequence", "tool_use", "pause_turn",
            "refusal", "model_context_window_exceeded") else "unknown"
        valid = reason == "end_turn"
        content = payload.get("content")
        if isinstance(content, list):
            for block in content:
                block = _mapping(block)
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    texts.append(block["text"])
                elif block.get("type") not in ("thinking", "redacted_thinking"):
                    valid = False
    else:
        status = payload.get("status")
        reason = status if status in (
            "completed", "incomplete", "failed", "cancelled", "queued", "in_progress") else "unknown"
        valid = status == "completed" and not payload.get("error")
        output = payload.get("output")
        if isinstance(output, list):
            for item in output:
                item = _mapping(item)
                if item.get("type") == "reasoning":
                    continue
                if (item.get("type") != "message" or item.get("role") != "assistant"
                        or item.get("status") != "completed"):
                    valid = False
                    continue
                blocks = item.get("content")
                if not isinstance(blocks, list):
                    valid = False
                    continue
                for block in blocks:
                    block = _mapping(block)
                    if block.get("type") == "output_text" and isinstance(block.get("text"), str):
                        texts.append(block["text"])
                    else:
                        valid = False
    result.update(predicted=(_label(texts[0]) if valid and len(texts) == 1
                             and result["usage_complete"] else "invalid"),
                  provider=spec["provider"], returned_model=returned, stop_reason=reason,
                  truncated=reason in ("max_tokens", "incomplete", "model_context_window_exceeded"))
    if returned is None:
        result.update(predicted="invalid", usage_complete=False, cost_is_lower_bound=True)
    return result


def classify(model, system, user, environment):
    """Make one HTTPS request using keys only from the supplied environment.

    No retries, redirects, proxy settings, custom endpoints, SDK persistence, or
    raw response metadata. A received but unaccounted response blocks safe resume
    via ``usage_complete=False`` or an ``ambiguous`` ProviderError.
    """
    if not isinstance(model, str) or model not in MODEL_SPECS:
        raise ProviderError("unsupported_model")
    if not isinstance(environment, Mapping):
        raise ProviderError("invalid_request")
    spec = MODEL_SPECS[model]
    key_name = "ANTHROPIC_API_KEY" if spec["provider"] == "anthropic" else "OPENAI_API_KEY"
    key = environment.get(key_name)
    if not isinstance(key, str) or not key.strip():
        raise ProviderError("missing_api_key")
    if (not isinstance(system, str) or not isinstance(user, str)
            or any(ord(char) < 33 or ord(char) > 126 for char in key)):
        raise ProviderError("invalid_request")
    body = {"model": model, **deepcopy(spec["settings"])}
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if spec["provider"] == "anthropic":
        body.update(system=system, messages=[{"role": "user", "content": user}])
        headers.update({"x-api-key": key, "anthropic-version": spec["api_version"]})
    else:
        body.update(instructions=system, input=[{"role": "user", "content": user}])
        headers["Authorization"] = "Bearer " + key
    try:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = request.Request(_ENDPOINTS[spec["provider"]], data=encoded,
                              headers=headers, method="POST")
        # Unlike create_default_context(), this never enables SSLKEYLOGFILE.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_default_certs()
        opener = request.build_opener(request.ProxyHandler({}), _NoRedirect(),
                                      request.HTTPSHandler(context=context))
    except (ValueError, TypeError, UnicodeError, OSError):
        raise ProviderError("invalid_request") from None
    try:
        with opener.open(req, timeout=_TIMEOUT) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except error.HTTPError as exc:
        status = exc.code
        safe_result = None
        rate_limited = False
        code = parameter = retry_after = None
        try:
            delay = exc.headers.get("Retry-After") if exc.headers else None
            retry_after = int(delay) if delay and delay.isdigit() else None
            raw = exc.read(_MAX_RESPONSE_BYTES + 1)
            payload = _json(raw) if len(raw) <= _MAX_RESPONSE_BYTES else None
            provider_error = _mapping(_mapping(payload).get("error"))
            codes = [value.lower() for value in (provider_error.get("type"),
                                                 provider_error.get("code"))
                     if isinstance(value, str)]
            code = provider_error.get("code") or provider_error.get("type")
            parameter = provider_error.get("param")
            rate_limited = (any(value in ("rate_limit_exceeded", "rate_limit_error")
                                for value in codes)
                            and not any(marker in value for value in codes
                                        for marker in ("quota", "billing", "payment", "credit")))
            if isinstance(payload, dict) and isinstance(payload.get("usage"), dict):
                safe_result = _result(model, payload)
                safe_result["predicted"] = "invalid"
        except (OSError, ValueError, TypeError, RecursionError, http.client.HTTPException):
            pass
        finally:
            exc.close()
        raise ProviderError("redirect_rejected" if 300 <= status < 400 else "http_error",
                            status_code=status,
                            retryable=(status == 429 and rate_limited)
                            or status in (408, 409) or status >= 500,
                            ambiguous=status == 408 or status >= 500 or safe_result is not None,
                            result=safe_result, retry_after=retry_after,
                            error_code=code, parameter=parameter) from None
    except (OSError, error.URLError, http.client.HTTPException, ValueError) as exc:
        reason = exc.reason if isinstance(exc, error.URLError) else exc
        cause = ("timeout" if isinstance(reason, TimeoutError) else
                 "dns" if isinstance(reason, socket.gaierror) else
                 "tls" if isinstance(reason, ssl.SSLError) else
                 "protocol" if isinstance(reason, (http.client.HTTPException, ValueError))
                 else "connection")
        raise ProviderError("transport_error", retryable=cause != "tls",
                            ambiguous=True, cause=cause) from None
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise ProviderError("response_too_large", ambiguous=True)
    try:
        payload = _json(raw)
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise ProviderError("invalid_response", ambiguous=True) from None
    if not isinstance(payload, dict):
        raise ProviderError("invalid_response", ambiguous=True)
    return _result(model, payload)

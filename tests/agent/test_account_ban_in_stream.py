"""An upstream account ban relayed inside an HTTP-200 SSE stream is permanent.

An aggregator (e.g. OpenRouter) can relay "this user has been blocked for a
previous policy violation" as an error object in a 200 stream.  The OpenAI SDK
raises a status-less ``APIError``; classified ``unknown`` it was retried as an
outage.  A numeric ``error.code`` in that object is also the HTTP status.
"""
from types import SimpleNamespace

import httpx
import openai
import pytest

from agent.error_classifier import (
    FailoverReason,
    _extract_status_code,
    classify_api_error,
)

BAN = (
    "Policy Violation: this user has been blocked for a previous policy violation. "
    "Learn more: https://platform.openai.com/docs/guides/safety-best-practices"
)
_REQ = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")


class _StreamError(Exception):
    """Status-less SDK error raised mid-stream; carries the SSE error body."""

    def __init__(self, message, body=None):
        super().__init__(message)
        self.body = body or {}
        self.response = SimpleNamespace(headers={})


@pytest.mark.parametrize("status", [None, 403])
def test_account_ban_is_permanent_policy_block(status):
    if status is None:
        err = openai.APIError(BAN, _REQ, body={"message": BAN, "code": 403})
    else:
        err = openai.PermissionDeniedError(
            BAN, response=httpx.Response(status, request=_REQ), body={"error": {"message": BAN}}
        )
    result = classify_api_error(err, provider="openrouter", model="openai/gpt-4.1-nano")
    assert result.reason == FailoverReason.provider_policy_blocked
    assert result.retryable is False
    assert result.should_fallback is True
    # Every key on a banned account is banned: rotating only burns credentials.
    assert result.should_rotate_credential is False


def test_ban_in_message_without_any_body_or_status():
    result = classify_api_error(_StreamError(BAN), provider="custom")
    assert result.reason == FailoverReason.provider_policy_blocked
    assert result.retryable is False


_BAN_BODY = {"error": {"code": 403, "message": "Your account has been suspended upstream"}}


def test_in_stream_numeric_code_is_the_status():
    assert _extract_status_code(_StreamError("Error code: 403", _BAN_BODY)) == 403


def test_exception_status_wins_over_body_code():
    err = openai.RateLimitError(
        "slow down", response=httpx.Response(429, request=_REQ), body=_BAN_BODY
    )
    assert _extract_status_code(err) == 429


def test_symbolic_and_non_error_codes_are_not_statuses():
    for code in ("insufficient_quota", "403", True, 403.0, 99, 200, 399, 600):
        body = {"error": {"code": code, "message": "x"}}
        assert _extract_status_code(_StreamError("x", body)) is None, code


def test_top_level_and_alt_keys_count():
    assert _extract_status_code(_StreamError("x", {"code": 503})) == 503
    assert _extract_status_code(_StreamError("x", {"error": {"http_status": 502}})) == 502


def test_in_stream_403_classifies_as_auth_not_transient():
    result = classify_api_error(_StreamError("Error code: 403", _BAN_BODY), provider="custom")
    assert result.status_code == 403
    assert result.reason in {FailoverReason.auth, FailoverReason.auth_permanent}
    assert result.retryable is False


def test_in_stream_502_stays_retryable():
    body = {"error": {"code": 502, "message": "upstream connect error"}}
    result = classify_api_error(_StreamError("Error code: 502", body), provider="custom")
    assert result.status_code == 502
    assert result.retryable is True


def test_statusless_bodyless_error_stays_unknown():
    result = classify_api_error(_StreamError("weird failure"), provider="custom")
    assert result.status_code is None
    assert result.reason == FailoverReason.unknown

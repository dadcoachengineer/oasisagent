"""Tests for the provider-agnostic LLM client."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import litellm
import pytest

from oasisagent.config import LlmConfig, LlmEndpointConfig, LlmOptionsConfig
from oasisagent.llm.client import LLMClient, LLMError, LLMRole, UsageStats

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> LlmConfig:
    defaults: dict[str, Any] = {
        "triage": LlmEndpointConfig(
            base_url="http://localhost:11434/v1",
            model="qwen2.5:7b",
            api_key="",
            timeout=30,
            max_tokens=1024,
            temperature=0.1,
        ),
        "reasoning": LlmEndpointConfig(
            base_url="https://api.anthropic.com",
            model="claude-sonnet-4-5-20250929",
            api_key="sk-test-key",
            timeout=45,
            max_tokens=4096,
            temperature=0.2,
        ),
        "options": LlmOptionsConfig(),
    }
    defaults.update(overrides)
    return LlmConfig(**defaults)


def _client(**overrides: Any) -> LLMClient:
    return LLMClient(_make_config(**overrides))


def _mock_response(
    content: str = "Hello",
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    total_tokens: int = 15,
    *,
    usage_present: bool = True,
) -> MagicMock:
    """Build a mock LiteLLM response object."""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = content
    if usage_present:
        response.usage = MagicMock()
        response.usage.prompt_tokens = prompt_tokens
        response.usage.completion_tokens = completion_tokens
        response.usage.total_tokens = total_tokens
    else:
        response.usage = None
    return response


_MESSAGES = [{"role": "user", "content": "test"}]


def _timeout() -> litellm.Timeout:
    return litellm.Timeout(message="timeout", model="test", llm_provider="test")


# ---------------------------------------------------------------------------
# Role routing
# ---------------------------------------------------------------------------


class TestRoleRouting:
    """Triage and reasoning route to their respective endpoints."""

    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_triage_uses_triage_config(self, mock_acomp: AsyncMock) -> None:
        mock_acomp.return_value = _mock_response()
        client = _client()

        await client.complete(LLMRole.TRIAGE, _MESSAGES)

        mock_acomp.assert_called_once()
        call_kwargs = mock_acomp.call_args.kwargs
        assert call_kwargs["model"] == "qwen2.5:7b"
        assert call_kwargs["api_base"] == "http://localhost:11434/v1"
        assert call_kwargs["max_tokens"] == 1024

    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_reasoning_uses_reasoning_config(self, mock_acomp: AsyncMock) -> None:
        mock_acomp.return_value = _mock_response()
        client = _client()

        await client.complete(LLMRole.REASONING, _MESSAGES)

        call_kwargs = mock_acomp.call_args.kwargs
        assert call_kwargs["model"] == "claude-sonnet-4-5-20250929"
        assert call_kwargs["api_base"] == "https://api.anthropic.com"
        assert call_kwargs["api_key"] == "sk-test-key"

    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_empty_api_key_not_passed(self, mock_acomp: AsyncMock) -> None:
        """Local models (triage) with empty api_key — kwarg omitted entirely."""
        mock_acomp.return_value = _mock_response()
        client = _client()

        await client.complete(LLMRole.TRIAGE, _MESSAGES)

        call_kwargs = mock_acomp.call_args.kwargs
        assert "api_key" not in call_kwargs

    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_temperature_override(self, mock_acomp: AsyncMock) -> None:
        mock_acomp.return_value = _mock_response()
        client = _client()

        await client.complete(LLMRole.TRIAGE, _MESSAGES, temperature=0.7)

        call_kwargs = mock_acomp.call_args.kwargs
        assert call_kwargs["temperature"] == 0.7

    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_default_temperature_from_config(self, mock_acomp: AsyncMock) -> None:
        mock_acomp.return_value = _mock_response()
        client = _client()

        await client.complete(LLMRole.TRIAGE, _MESSAGES)

        call_kwargs = mock_acomp.call_args.kwargs
        assert call_kwargs["temperature"] == 0.1  # triage default


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


class TestResponseParsing:
    """LLMResponse correctly extracts content, usage, and metadata."""

    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_response_content(self, mock_acomp: AsyncMock) -> None:
        mock_acomp.return_value = _mock_response(content="The answer is 42")
        client = _client()

        result = await client.complete(LLMRole.TRIAGE, _MESSAGES)

        assert result.content == "The answer is 42"
        assert result.role == LLMRole.TRIAGE
        assert result.model == "qwen2.5:7b"
        assert result.degraded is False

    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_response_with_usage(self, mock_acomp: AsyncMock) -> None:
        mock_acomp.return_value = _mock_response(
            prompt_tokens=100, completion_tokens=50, total_tokens=150
        )
        client = _client()

        result = await client.complete(LLMRole.TRIAGE, _MESSAGES)

        assert result.usage is not None
        assert result.usage.prompt_tokens == 100
        assert result.usage.completion_tokens == 50
        assert result.usage.total_tokens == 150

    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_response_without_usage(self, mock_acomp: AsyncMock) -> None:
        """Some providers don't return usage — should be None, not zeros."""
        mock_acomp.return_value = _mock_response(usage_present=False)
        client = _client()

        result = await client.complete(LLMRole.TRIAGE, _MESSAGES)

        assert result.usage is None

    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_latency_measured(self, mock_acomp: AsyncMock) -> None:
        mock_acomp.return_value = _mock_response()
        client = _client()

        result = await client.complete(LLMRole.TRIAGE, _MESSAGES)

        assert result.latency_ms >= 0

    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_none_content_becomes_empty_string(self, mock_acomp: AsyncMock) -> None:
        response = _mock_response()
        response.choices[0].message.content = None
        mock_acomp.return_value = response
        client = _client()

        result = await client.complete(LLMRole.TRIAGE, _MESSAGES)

        assert result.content == ""


# ---------------------------------------------------------------------------
# Structured output
# ---------------------------------------------------------------------------


class TestStructuredOutput:
    """response_format is passed through to LiteLLM."""

    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_response_format_passed(self, mock_acomp: AsyncMock) -> None:
        mock_acomp.return_value = _mock_response()
        client = _client()

        class MySchema:
            pass

        await client.complete(LLMRole.TRIAGE, _MESSAGES, response_format=MySchema)

        call_kwargs = mock_acomp.call_args.kwargs
        assert call_kwargs["response_format"] is MySchema

    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_no_response_format_not_in_kwargs(self, mock_acomp: AsyncMock) -> None:
        mock_acomp.return_value = _mock_response()
        client = _client()

        await client.complete(LLMRole.TRIAGE, _MESSAGES)

        call_kwargs = mock_acomp.call_args.kwargs
        assert "response_format" not in call_kwargs


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------


class TestRetries:
    """Transient errors are retried; non-retryable errors raise immediately."""

    @patch("oasisagent.llm.client.asyncio.sleep", new_callable=AsyncMock)
    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_retry_on_timeout(
        self, mock_acomp: AsyncMock, mock_sleep: AsyncMock
    ) -> None:
        mock_acomp.side_effect = [
            _timeout(),
            _mock_response(content="recovered"),
        ]
        client = _client(options=LlmOptionsConfig(retry_attempts=2, fallback_to_triage=False))

        result = await client.complete(LLMRole.TRIAGE, _MESSAGES)

        assert result.content == "recovered"
        assert mock_acomp.call_count == 2
        mock_sleep.assert_called_once_with(0.5)  # 0.5 * 2^0

    @patch("oasisagent.llm.client.asyncio.sleep", new_callable=AsyncMock)
    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_retry_on_rate_limit(
        self, mock_acomp: AsyncMock, mock_sleep: AsyncMock
    ) -> None:
        mock_acomp.side_effect = [
            litellm.RateLimitError(
                message="rate limited", model="test", llm_provider="test"
            ),
            litellm.RateLimitError(
                message="rate limited", model="test", llm_provider="test"
            ),
            _mock_response(content="ok"),
        ]
        client = _client(options=LlmOptionsConfig(retry_attempts=2, fallback_to_triage=False))

        result = await client.complete(LLMRole.TRIAGE, _MESSAGES)

        assert result.content == "ok"
        assert mock_acomp.call_count == 3  # 1 initial + 2 retries
        # Backoff: 0.5s, 1.0s
        assert mock_sleep.call_args_list[0].args == (0.5,)
        assert mock_sleep.call_args_list[1].args == (1.0,)

    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_auth_error_no_retry(self, mock_acomp: AsyncMock) -> None:
        mock_acomp.side_effect = litellm.AuthenticationError(
            message="bad key", model="test", llm_provider="test"
        )
        client = _client(options=LlmOptionsConfig(retry_attempts=3, fallback_to_triage=False))

        with pytest.raises(litellm.AuthenticationError):
            await client.complete(LLMRole.TRIAGE, _MESSAGES)

        assert mock_acomp.call_count == 1

    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_bad_request_no_retry(self, mock_acomp: AsyncMock) -> None:
        mock_acomp.side_effect = litellm.BadRequestError(
            message="bad request", model="test", llm_provider="test"
        )
        client = _client(options=LlmOptionsConfig(retry_attempts=3, fallback_to_triage=False))

        with pytest.raises(litellm.BadRequestError):
            await client.complete(LLMRole.TRIAGE, _MESSAGES)

        assert mock_acomp.call_count == 1

    @patch("oasisagent.llm.client.asyncio.sleep", new_callable=AsyncMock)
    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_retries_exhausted_raises(
        self, mock_acomp: AsyncMock, mock_sleep: AsyncMock
    ) -> None:
        mock_acomp.side_effect = _timeout()
        client = _client(options=LlmOptionsConfig(retry_attempts=2, fallback_to_triage=False))

        with pytest.raises(LLMError, match="after 2 retries"):
            await client.complete(LLMRole.TRIAGE, _MESSAGES)

        assert mock_acomp.call_count == 3  # 1 initial + 2 retries


# ---------------------------------------------------------------------------
# Fallback (reasoning → triage)
# ---------------------------------------------------------------------------


class TestFallback:
    """Reasoning failures fall back to triage when configured."""

    @patch("oasisagent.llm.client.asyncio.sleep", new_callable=AsyncMock)
    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_fallback_after_retries_exhausted(
        self, mock_acomp: AsyncMock, mock_sleep: AsyncMock
    ) -> None:
        """Reasoning retries N times, THEN falls back to triage."""
        timeout_err = _timeout()
        mock_acomp.side_effect = [
            # 3 reasoning attempts (1 initial + 2 retries)
            timeout_err,
            timeout_err,
            timeout_err,
            # 1 triage attempt (fallback)
            _mock_response(content="fallback response"),
        ]
        client = _client(
            options=LlmOptionsConfig(retry_attempts=2, fallback_to_triage=True)
        )

        result = await client.complete(LLMRole.REASONING, _MESSAGES)

        assert result.content == "fallback response"
        assert result.degraded is True
        # 3 reasoning + 1 triage = 4 total calls
        assert mock_acomp.call_count == 4

    @patch("oasisagent.llm.client.asyncio.sleep", new_callable=AsyncMock)
    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_fallback_uses_triage_endpoint(
        self, mock_acomp: AsyncMock, mock_sleep: AsyncMock
    ) -> None:
        """Fallback call uses triage endpoint config, not reasoning."""
        mock_acomp.side_effect = [
            _timeout(),
            _mock_response(content="triage response"),
        ]
        client = _client(
            options=LlmOptionsConfig(retry_attempts=0, fallback_to_triage=True)
        )

        result = await client.complete(LLMRole.REASONING, _MESSAGES)

        # First call = reasoning endpoint, second = triage endpoint
        calls = mock_acomp.call_args_list
        assert calls[0].kwargs["model"] == "claude-sonnet-4-5-20250929"
        assert calls[1].kwargs["model"] == "qwen2.5:7b"
        assert result.degraded is True

    @patch("oasisagent.llm.client.asyncio.sleep", new_callable=AsyncMock)
    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_no_fallback_when_disabled(
        self, mock_acomp: AsyncMock, mock_sleep: AsyncMock
    ) -> None:
        mock_acomp.side_effect = _timeout()
        client = _client(
            options=LlmOptionsConfig(retry_attempts=1, fallback_to_triage=False)
        )

        with pytest.raises(LLMError):
            await client.complete(LLMRole.REASONING, _MESSAGES)

        # 1 initial + 1 retry = 2 calls (no triage fallback)
        assert mock_acomp.call_count == 2

    @patch("oasisagent.llm.client.asyncio.sleep", new_callable=AsyncMock)
    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_triage_role_no_fallback(
        self, mock_acomp: AsyncMock, mock_sleep: AsyncMock
    ) -> None:
        """Triage failures don't fall back — there's nothing below triage."""
        mock_acomp.side_effect = _timeout()
        client = _client(
            options=LlmOptionsConfig(retry_attempts=0, fallback_to_triage=True)
        )

        with pytest.raises(LLMError):
            await client.complete(LLMRole.TRIAGE, _MESSAGES)

        assert mock_acomp.call_count == 1

    @patch("oasisagent.llm.client.asyncio.sleep", new_callable=AsyncMock)
    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_both_endpoints_fail_raises(
        self, mock_acomp: AsyncMock, mock_sleep: AsyncMock
    ) -> None:
        """Reasoning fails, triage also fails → LLMError."""
        mock_acomp.side_effect = _timeout()
        client = _client(
            options=LlmOptionsConfig(retry_attempts=0, fallback_to_triage=True)
        )

        with pytest.raises(LLMError, match="Both reasoning and triage"):
            await client.complete(LLMRole.REASONING, _MESSAGES)

        # 1 reasoning + 1 triage
        assert mock_acomp.call_count == 2

    @patch("oasisagent.llm.client.asyncio.sleep", new_callable=AsyncMock)
    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_fallback_retries_sequence(
        self, mock_acomp: AsyncMock, mock_sleep: AsyncMock
    ) -> None:
        """Verify exact sequence: reasoning x3 → triage x1."""
        timeout_err = _timeout()
        mock_acomp.side_effect = [
            timeout_err, timeout_err, timeout_err,  # reasoning: 1 + 2 retries
            _mock_response(content="ok"),            # triage: 1st attempt
        ]
        client = _client(
            options=LlmOptionsConfig(retry_attempts=2, fallback_to_triage=True)
        )

        result = await client.complete(LLMRole.REASONING, _MESSAGES)

        # Verify models called in order
        models = [c.kwargs["model"] for c in mock_acomp.call_args_list]
        assert models == [
            "claude-sonnet-4-5-20250929",  # reasoning attempt 1
            "claude-sonnet-4-5-20250929",  # reasoning retry 1
            "claude-sonnet-4-5-20250929",  # reasoning retry 2
            "qwen2.5:7b",                 # triage fallback
        ]
        assert result.degraded is True


# ---------------------------------------------------------------------------
# Usage stats
# ---------------------------------------------------------------------------


class TestUsageStats:
    """Usage tracking accumulates across calls."""

    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_usage_accumulates(self, mock_acomp: AsyncMock) -> None:
        mock_acomp.return_value = _mock_response(
            prompt_tokens=10, completion_tokens=5, total_tokens=15
        )
        client = _client()

        await client.complete(LLMRole.TRIAGE, _MESSAGES)
        await client.complete(LLMRole.TRIAGE, _MESSAGES)

        stats = client.get_usage_stats()
        assert stats["triage"].total_requests == 2
        assert stats["triage"].total_prompt_tokens == 20
        assert stats["triage"].total_completion_tokens == 10
        assert stats["triage"].total_tokens == 30
        # Reasoning untouched
        assert stats["reasoning"].total_requests == 0

    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_request_counted_even_without_usage(self, mock_acomp: AsyncMock) -> None:
        """No usage in response → request counted, but token stats stay zero."""
        mock_acomp.return_value = _mock_response(usage_present=False)
        client = _client()

        await client.complete(LLMRole.TRIAGE, _MESSAGES)

        stats = client.get_usage_stats()
        assert stats["triage"].total_requests == 1
        assert stats["triage"].total_tokens == 0

    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_per_role_isolation(self, mock_acomp: AsyncMock) -> None:
        mock_acomp.return_value = _mock_response(
            prompt_tokens=100, completion_tokens=50, total_tokens=150
        )
        client = _client()

        await client.complete(LLMRole.TRIAGE, _MESSAGES)
        await client.complete(LLMRole.REASONING, _MESSAGES)

        stats = client.get_usage_stats()
        assert stats["triage"].total_requests == 1
        assert stats["reasoning"].total_requests == 1

    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_cost_tracking_disabled(self, mock_acomp: AsyncMock) -> None:
        mock_acomp.return_value = _mock_response(
            prompt_tokens=10, completion_tokens=5, total_tokens=15
        )
        client = _client(options=LlmOptionsConfig(cost_tracking=False))

        await client.complete(LLMRole.TRIAGE, _MESSAGES)

        stats = client.get_usage_stats()
        assert stats["triage"].total_requests == 0

    def test_empty_stats(self) -> None:
        client = _client()
        stats = client.get_usage_stats()

        assert stats["triage"] == UsageStats()
        assert stats["reasoning"] == UsageStats()


# ---------------------------------------------------------------------------
# Prompt logging
# ---------------------------------------------------------------------------


class TestPromptLogging:
    """log_prompts gates debug logging of messages."""

    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_prompts_logged_when_enabled(
        self, mock_acomp: AsyncMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_acomp.return_value = _mock_response()
        client = _client(options=LlmOptionsConfig(log_prompts=True))

        import logging

        with caplog.at_level(logging.DEBUG, logger="oasisagent.llm.client"):
            await client.complete(LLMRole.TRIAGE, _MESSAGES)

        assert any("LLM prompt" in record.message for record in caplog.records)

    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_prompts_not_logged_when_disabled(
        self, mock_acomp: AsyncMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_acomp.return_value = _mock_response()
        client = _client(options=LlmOptionsConfig(log_prompts=False))

        import logging

        with caplog.at_level(logging.DEBUG, logger="oasisagent.llm.client"):
            await client.complete(LLMRole.TRIAGE, _MESSAGES)

        assert not any("LLM prompt" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Health tracking (last call outcome)
# ---------------------------------------------------------------------------


class TestRoleHealth:
    """get_role_health() infers status from last call outcome."""

    def test_initial_state_unknown(self) -> None:
        client = _client()
        assert client.get_role_health(LLMRole.TRIAGE) == "unknown"
        assert client.get_role_health(LLMRole.REASONING) == "unknown"

    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_successful_call_sets_connected(self, mock_acomp: AsyncMock) -> None:
        mock_acomp.return_value = _mock_response()
        client = _client()

        await client.complete(LLMRole.TRIAGE, _MESSAGES)

        assert client.get_role_health(LLMRole.TRIAGE) == "connected"
        assert client.get_role_health(LLMRole.REASONING) == "unknown"

    @patch("oasisagent.llm.client.asyncio.sleep", new_callable=AsyncMock)
    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_failed_call_sets_error(
        self, mock_acomp: AsyncMock, mock_sleep: AsyncMock
    ) -> None:
        mock_acomp.side_effect = _timeout()
        client = _client(options=LlmOptionsConfig(retry_attempts=0, fallback_to_triage=False))

        with pytest.raises(LLMError):
            await client.complete(LLMRole.TRIAGE, _MESSAGES)

        assert client.get_role_health(LLMRole.TRIAGE) == "error"

    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_auth_error_sets_error(self, mock_acomp: AsyncMock) -> None:
        mock_acomp.side_effect = litellm.AuthenticationError(
            message="bad key", model="test", llm_provider="test"
        )
        client = _client(options=LlmOptionsConfig(retry_attempts=0, fallback_to_triage=False))

        with pytest.raises(litellm.AuthenticationError):
            await client.complete(LLMRole.TRIAGE, _MESSAGES)

        assert client.get_role_health(LLMRole.TRIAGE) == "error"

    @patch("oasisagent.llm.client.asyncio.sleep", new_callable=AsyncMock)
    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_fallback_sets_both_roles(
        self, mock_acomp: AsyncMock, mock_sleep: AsyncMock
    ) -> None:
        """Reasoning fails, triage fallback succeeds → reasoning=error, triage=connected."""
        mock_acomp.side_effect = [
            _timeout(),  # reasoning fails
            _mock_response(content="fallback"),  # triage succeeds
        ]
        client = _client(options=LlmOptionsConfig(retry_attempts=0, fallback_to_triage=True))

        await client.complete(LLMRole.REASONING, _MESSAGES)

        assert client.get_role_health(LLMRole.REASONING) == "error"
        assert client.get_role_health(LLMRole.TRIAGE) == "connected"

    @patch("oasisagent.llm.client.asyncio.sleep", new_callable=AsyncMock)
    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_both_fail_sets_both_error(
        self, mock_acomp: AsyncMock, mock_sleep: AsyncMock
    ) -> None:
        """Both reasoning and triage fail → both error."""
        mock_acomp.side_effect = _timeout()
        client = _client(options=LlmOptionsConfig(retry_attempts=0, fallback_to_triage=True))

        with pytest.raises(LLMError):
            await client.complete(LLMRole.REASONING, _MESSAGES)

        assert client.get_role_health(LLMRole.REASONING) == "error"
        assert client.get_role_health(LLMRole.TRIAGE) == "error"

    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_recovery_after_failure(self, mock_acomp: AsyncMock) -> None:
        """A successful call after a failure flips status back to connected."""
        client = _client(options=LlmOptionsConfig(retry_attempts=0, fallback_to_triage=False))

        # First call fails
        mock_acomp.side_effect = litellm.AuthenticationError(
            message="bad key", model="test", llm_provider="test"
        )
        with pytest.raises(litellm.AuthenticationError):
            await client.complete(LLMRole.TRIAGE, _MESSAGES)
        assert client.get_role_health(LLMRole.TRIAGE) == "error"

        # Second call succeeds
        mock_acomp.side_effect = None
        mock_acomp.return_value = _mock_response()
        await client.complete(LLMRole.TRIAGE, _MESSAGES)
        assert client.get_role_health(LLMRole.TRIAGE) == "connected"


# ---------------------------------------------------------------------------
# Warm-up (health probe at startup)
# ---------------------------------------------------------------------------


class TestWarmUp:
    """warm_up() sends minimal probes to each role and logs results."""

    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_both_roles_healthy(self, mock_acomp: AsyncMock) -> None:
        mock_acomp.return_value = _mock_response(content="OK")
        client = _client()

        results = await client.warm_up()

        assert results == {LLMRole.TRIAGE: True, LLMRole.REASONING: True}
        assert client.get_role_health(LLMRole.TRIAGE) == "connected"
        assert client.get_role_health(LLMRole.REASONING) == "connected"
        # Two calls: one per role
        assert mock_acomp.call_count == 2

    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_one_role_fails(self, mock_acomp: AsyncMock) -> None:
        """Triage succeeds, reasoning fails — warm_up doesn't raise."""
        mock_acomp.side_effect = [
            _mock_response(content="OK"),  # triage
            litellm.APIConnectionError(
                message="connection refused", model="test", llm_provider="test"
            ),  # reasoning
        ]
        client = _client()

        results = await client.warm_up()

        assert results[LLMRole.TRIAGE] is True
        assert results[LLMRole.REASONING] is False
        assert client.get_role_health(LLMRole.TRIAGE) == "connected"
        assert client.get_role_health(LLMRole.REASONING) == "error"

    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_both_roles_fail(self, mock_acomp: AsyncMock) -> None:
        """Both fail — warm_up still doesn't raise."""
        mock_acomp.side_effect = _timeout()
        client = _client()

        results = await client.warm_up()

        assert results == {LLMRole.TRIAGE: False, LLMRole.REASONING: False}
        assert client.get_role_health(LLMRole.TRIAGE) == "error"
        assert client.get_role_health(LLMRole.REASONING) == "error"

    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_probe_uses_max_tokens_1(self, mock_acomp: AsyncMock) -> None:
        """Probe uses max_tokens=1 to minimize cost."""
        mock_acomp.return_value = _mock_response(content="OK")
        client = _client()

        await client.warm_up()

        for call in mock_acomp.call_args_list:
            assert call.kwargs["max_tokens"] == 1

    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_probe_uses_correct_endpoints(self, mock_acomp: AsyncMock) -> None:
        """Each probe uses the real endpoint config for its role."""
        mock_acomp.return_value = _mock_response(content="OK")
        client = _client()

        await client.warm_up()

        calls = mock_acomp.call_args_list
        # First call = triage
        assert calls[0].kwargs["model"] == "qwen2.5:7b"
        assert calls[0].kwargs["api_base"] == "http://localhost:11434/v1"
        # Second call = reasoning
        assert calls[1].kwargs["model"] == "claude-sonnet-4-5-20250929"
        assert calls[1].kwargs["api_base"] == "https://api.anthropic.com"
        assert calls[1].kwargs["api_key"] == "sk-test-key"

    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_probe_does_not_count_usage(self, mock_acomp: AsyncMock) -> None:
        """Warm-up probes bypass usage tracking."""
        mock_acomp.return_value = _mock_response(content="OK")
        client = _client()

        await client.warm_up()

        stats = client.get_usage_stats()
        assert stats["triage"].total_requests == 0
        assert stats["reasoning"].total_requests == 0

    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_logs_summary(
        self, mock_acomp: AsyncMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Warm-up logs a single summary line."""
        mock_acomp.return_value = _mock_response(content="OK")
        client = _client()

        import logging

        with caplog.at_level(logging.INFO, logger="oasisagent.llm.client"):
            await client.warm_up()

        assert any("LLM warm-up" in record.message for record in caplog.records)
        summary = next(r for r in caplog.records if "LLM warm-up" in r.message)
        assert "triage=ok" in summary.message
        assert "reasoning=ok" in summary.message

    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_logs_failure_detail(
        self, mock_acomp: AsyncMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Failed probes include error description in summary."""
        mock_acomp.side_effect = _timeout()
        client = _client()

        import logging

        with caplog.at_level(logging.INFO, logger="oasisagent.llm.client"):
            await client.warm_up()

        # Find the INFO-level summary line (not the per-role WARNING lines)
        summary = next(
            r for r in caplog.records
            if "LLM warm-up:" in r.message and r.levelno == logging.INFO
        )
        assert "triage=FAILED" in summary.message
        assert "reasoning=FAILED" in summary.message

    @patch("oasisagent.llm.client.litellm.acompletion", new_callable=AsyncMock)
    async def test_timeout_capped_at_15s(self, mock_acomp: AsyncMock) -> None:
        """Probe timeout is capped at 15s even if endpoint timeout is higher."""
        mock_acomp.return_value = _mock_response(content="OK")
        config = _make_config(
            reasoning=LlmEndpointConfig(
                base_url="https://api.anthropic.com",
                model="claude-sonnet-4-5-20250929",
                api_key="sk-test-key",
                timeout=120,  # very long timeout
                max_tokens=4096,
                temperature=0.2,
            ),
        )
        client = LLMClient(config)

        await client.warm_up()

        # Reasoning probe timeout should be capped at 15
        reasoning_call = mock_acomp.call_args_list[1]
        assert reasoning_call.kwargs["timeout"] == 15

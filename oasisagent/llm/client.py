"""Provider-agnostic LLM client with role-based routing.

Wraps LiteLLM to provide a clean interface for the rest of the codebase.
The decision engine and prompt layers call ``LLMClient.complete(role, messages)``
and never reference LiteLLM directly.

ARCHITECTURE.md §7 describes the LLM client design.
"""

from __future__ import annotations

import asyncio
import logging
import time
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import litellm
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from oasisagent.config import LlmConfig, LlmEndpointConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Retryable vs. immediate-raise exceptions
# ---------------------------------------------------------------------------

_RETRYABLE_ERRORS: tuple[type[Exception], ...] = (
    litellm.Timeout,
    litellm.RateLimitError,
    litellm.ServiceUnavailableError,
    litellm.APIConnectionError,
    litellm.InternalServerError,
)

_NON_RETRYABLE_ERRORS: tuple[type[Exception], ...] = (
    litellm.AuthenticationError,
    litellm.BadRequestError,
    litellm.NotFoundError,
    litellm.ContentPolicyViolationError,
)


# ---------------------------------------------------------------------------
# Public models
# ---------------------------------------------------------------------------


class LLMRole(StrEnum):
    """Which LLM endpoint to route to."""

    TRIAGE = "triage"
    REASONING = "reasoning"


class TokenUsage(BaseModel):
    """Token counts from a single completion."""

    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class LLMResponse(BaseModel):
    """Result of a single LLM completion call."""

    model_config = ConfigDict(extra="forbid")

    content: str
    role: LLMRole
    model: str
    usage: TokenUsage | None = None
    latency_ms: float
    degraded: bool = False


class UsageStats(BaseModel):
    """Cumulative usage statistics for one role."""

    model_config = ConfigDict(extra="forbid")

    total_requests: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0


# ---------------------------------------------------------------------------
# LLM Client
# ---------------------------------------------------------------------------


class LLMClient:
    """Provider-agnostic LLM client with role-based routing.

    Routes completion requests to configured endpoints based on
    :class:`LLMRole`. Handles retries, fallback, and usage tracking
    internally.

    The rest of the codebase interacts with LLMs exclusively through
    this class — no direct LiteLLM imports elsewhere.
    """

    def __init__(self, config: LlmConfig) -> None:
        self._config = config
        self._usage: dict[LLMRole, UsageStats] = {
            LLMRole.TRIAGE: UsageStats(),
            LLMRole.REASONING: UsageStats(),
        }
        # Track last call outcome per role: None = no calls yet
        self._last_call_ok: dict[LLMRole, bool | None] = {
            LLMRole.TRIAGE: None,
            LLMRole.REASONING: None,
        }

    async def complete(
        self,
        role: LLMRole,
        messages: list[dict[str, Any]],
        *,
        response_format: type | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        """Send a completion request to the configured endpoint for *role*.

        Args:
            role: Which endpoint to use (triage or reasoning).
            messages: Chat messages in OpenAI format.
            response_format: Optional Pydantic model or JSON schema for
                structured output. Passed through to LiteLLM.
            temperature: Override the configured temperature if provided.

        Returns:
            LLMResponse with content, usage, and metadata.

        Raises:
            litellm.AuthenticationError: Bad API key (immediate, no retry).
            litellm.BadRequestError: Malformed request (immediate, no retry).
            LLMError: All retries (and fallback if applicable) exhausted.
        """
        endpoint = self._endpoint_for(role)
        retry_attempts = self._config.options.retry_attempts

        if self._config.options.log_prompts:
            logger.debug("LLM prompt [%s]: %s", role.value, messages)

        # Try primary endpoint with retries
        try:
            result = await self._complete_with_retries(
                role=role,
                endpoint=endpoint,
                messages=messages,
                response_format=response_format,
                temperature=temperature,
                max_retries=retry_attempts,
            )
            self._last_call_ok[role] = True
            return result
        except _NON_RETRYABLE_ERRORS:
            self._last_call_ok[role] = False
            raise
        except Exception as primary_err:
            # Fallback: reasoning → triage (only after retries exhausted)
            if role == LLMRole.REASONING and self._config.options.fallback_to_triage:
                logger.warning(
                    "Reasoning endpoint failed after %d retries (%s), "
                    "falling back to triage endpoint",
                    retry_attempts,
                    primary_err,
                )
                self._last_call_ok[LLMRole.REASONING] = False
                triage_endpoint = self._endpoint_for(LLMRole.TRIAGE)
                try:
                    response = await self._complete_with_retries(
                        role=LLMRole.TRIAGE,
                        endpoint=triage_endpoint,
                        messages=messages,
                        response_format=response_format,
                        temperature=temperature,
                        max_retries=retry_attempts,
                    )
                    self._last_call_ok[LLMRole.TRIAGE] = True
                    # Mark as degraded — caller/audit can use this
                    return response.model_copy(update={"degraded": True})
                except Exception as fallback_err:
                    self._last_call_ok[LLMRole.TRIAGE] = False
                    raise LLMError(
                        f"Both reasoning and triage endpoints failed. "
                        f"Reasoning: {primary_err}. Triage: {fallback_err}"
                    ) from fallback_err

            self._last_call_ok[role] = False
            raise LLMError(
                f"LLM completion failed for role '{role.value}' "
                f"after {retry_attempts} retries: {primary_err}"
            ) from primary_err

    def get_usage_stats(self) -> dict[str, UsageStats]:
        """Return cumulative token usage per role."""
        return {role.value: stats for role, stats in self._usage.items()}

    async def warm_up(self) -> dict[LLMRole, bool]:
        """Send a minimal probe prompt to each configured role.

        Validates the full LLM path (endpoint, auth, model) by sending a
        single-token completion request. Results are logged and update the
        ``_last_call_ok`` tracker so ``get_role_health()`` reflects real
        status immediately after startup.

        Never raises — failed probes are logged as warnings and the
        corresponding role is marked unhealthy. Probe calls are not
        counted toward usage statistics.
        """
        results: dict[LLMRole, bool] = {}
        probe_messages = [{"role": "user", "content": "Respond with OK"}]
        parts: list[str] = []

        for role in LLMRole:
            endpoint = self._endpoint_for(role)
            start = time.monotonic()
            try:
                kwargs: dict[str, Any] = {
                    "model": endpoint.model,
                    "messages": probe_messages,
                    "api_base": endpoint.base_url,
                    "timeout": min(endpoint.timeout, 15),
                    "max_tokens": 1,
                    "temperature": 0,
                }
                if endpoint.api_key:
                    kwargs["api_key"] = endpoint.api_key

                await litellm.acompletion(**kwargs)
                latency_ms = (time.monotonic() - start) * 1000
                self._last_call_ok[role] = True
                results[role] = True
                parts.append(f"{role.value}=ok ({latency_ms:.0f}ms)")
            except Exception as exc:
                latency_ms = (time.monotonic() - start) * 1000
                self._last_call_ok[role] = False
                results[role] = False
                # Use the exception type + message for concise logging
                err_desc = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                parts.append(f"{role.value}=FAILED ({err_desc})")
                logger.warning(
                    "LLM warm-up probe failed for %s (%s, %.0fms): %s",
                    role.value,
                    endpoint.model,
                    latency_ms,
                    exc,
                )

        logger.info("LLM warm-up: %s", ", ".join(parts))
        return results

    def get_role_health(self, role: LLMRole) -> str:
        """Return health status for a role based on last call outcome.

        Returns ``"unknown"`` if no calls have been made yet,
        ``"connected"`` if the last call succeeded, or
        ``"error"`` if the last call failed.
        """
        last = self._last_call_ok.get(role)
        if last is None:
            return "unknown"
        return "connected" if last else "error"

    # -------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------

    def _endpoint_for(self, role: LLMRole) -> LlmEndpointConfig:
        """Get the endpoint config for a role."""
        if role == LLMRole.TRIAGE:
            return self._config.triage
        return self._config.reasoning

    async def _complete_with_retries(
        self,
        *,
        role: LLMRole,
        endpoint: LlmEndpointConfig,
        messages: list[dict[str, Any]],
        response_format: type | None,
        temperature: float | None,
        max_retries: int,
    ) -> LLMResponse:
        """Call LiteLLM with exponential backoff on retryable errors."""
        last_error: Exception | None = None

        for attempt in range(max_retries + 1):  # 0 retries = 1 attempt
            if attempt > 0:
                delay = 0.5 * (2 ** (attempt - 1))
                logger.debug(
                    "LLM retry %d/%d for %s (%.1fs backoff)",
                    attempt,
                    max_retries,
                    role.value,
                    delay,
                )
                await asyncio.sleep(delay)

            try:
                return await self._call_litellm(
                    role=role,
                    endpoint=endpoint,
                    messages=messages,
                    response_format=response_format,
                    temperature=temperature,
                )
            except _NON_RETRYABLE_ERRORS:
                raise
            except _RETRYABLE_ERRORS as exc:
                last_error = exc
                logger.warning(
                    "LLM transient error on attempt %d/%d for %s: %s",
                    attempt + 1,
                    max_retries + 1,
                    role.value,
                    exc,
                )

        raise last_error  # type: ignore[misc]

    async def _call_litellm(
        self,
        *,
        role: LLMRole,
        endpoint: LlmEndpointConfig,
        messages: list[dict[str, Any]],
        response_format: type | None,
        temperature: float | None,
    ) -> LLMResponse:
        """Single LiteLLM acompletion call."""
        kwargs: dict[str, Any] = {
            "model": endpoint.model,
            "messages": messages,
            "api_base": endpoint.base_url,
            "timeout": endpoint.timeout,
            "max_tokens": endpoint.max_tokens,
            "temperature": temperature if temperature is not None else endpoint.temperature,
        }

        # Only pass api_key if configured (local models may not need one)
        if endpoint.api_key:
            kwargs["api_key"] = endpoint.api_key

        if response_format is not None:
            kwargs["response_format"] = response_format

        start = time.monotonic()
        response = await litellm.acompletion(**kwargs)
        latency_ms = (time.monotonic() - start) * 1000

        content = response.choices[0].message.content or ""

        # Extract usage if present
        usage: TokenUsage | None = None
        if response.usage is not None:
            usage = TokenUsage(
                prompt_tokens=response.usage.prompt_tokens or 0,
                completion_tokens=response.usage.completion_tokens or 0,
                total_tokens=response.usage.total_tokens or 0,
            )

        # Accumulate stats — request always counted, tokens only when present
        if self._config.options.cost_tracking:
            stats = self._usage[role]
            stats.total_requests += 1
            if usage is not None:
                stats.total_prompt_tokens += usage.prompt_tokens
                stats.total_completion_tokens += usage.completion_tokens
                stats.total_tokens += usage.total_tokens

        result = LLMResponse(
            content=content,
            role=role,
            model=endpoint.model,
            usage=usage,
            latency_ms=latency_ms,
        )

        logger.debug(
            "LLM [%s] %s: %d tokens in %.0fms",
            role.value,
            endpoint.model,
            usage.total_tokens if usage else 0,
            latency_ms,
        )

        return result


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Raised when all LLM attempts (including fallback) are exhausted."""

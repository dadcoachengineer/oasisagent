"""Tests for the exponential backoff utility."""

from __future__ import annotations

from unittest.mock import patch

from oasisagent.backoff import ExponentialBackoff


class TestExponentialBackoff:
    """Tests for ExponentialBackoff."""

    async def test_initial_delay(self) -> None:
        backoff = ExponentialBackoff(initial=1.0, maximum=60.0, name="test")
        with patch("oasisagent.backoff.asyncio.sleep") as mock_sleep:
            await backoff.wait()
            mock_sleep.assert_called_once_with(1.0)

    async def test_exponential_increase(self) -> None:
        backoff = ExponentialBackoff(initial=1.0, maximum=60.0, multiplier=2.0)
        delays: list[float] = []

        with patch("oasisagent.backoff.asyncio.sleep") as mock_sleep:
            mock_sleep.side_effect = lambda d: delays.append(d)
            await backoff.wait()  # 1.0
            await backoff.wait()  # 2.0
            await backoff.wait()  # 4.0

        assert delays == [1.0, 2.0, 4.0]

    async def test_maximum_cap(self) -> None:
        backoff = ExponentialBackoff(initial=30.0, maximum=60.0, multiplier=2.0)
        delays: list[float] = []

        with patch("oasisagent.backoff.asyncio.sleep") as mock_sleep:
            mock_sleep.side_effect = lambda d: delays.append(d)
            await backoff.wait()  # 30
            await backoff.wait()  # 60 (capped)
            await backoff.wait()  # 60 (capped)

        assert delays == [30.0, 60.0, 60.0]

    async def test_reset(self) -> None:
        backoff = ExponentialBackoff(initial=1.0, maximum=60.0)

        with patch("oasisagent.backoff.asyncio.sleep"):
            await backoff.wait()
            await backoff.wait()
            assert backoff.attempts == 2

        backoff.reset()
        assert backoff.attempts == 0

        delays: list[float] = []
        with patch("oasisagent.backoff.asyncio.sleep") as mock_sleep:
            mock_sleep.side_effect = lambda d: delays.append(d)
            await backoff.wait()

        assert delays == [1.0]

    async def test_attempts_counter(self) -> None:
        backoff = ExponentialBackoff(initial=1.0)
        assert backoff.attempts == 0

        with patch("oasisagent.backoff.asyncio.sleep"):
            await backoff.wait()
            assert backoff.attempts == 1
            await backoff.wait()
            assert backoff.attempts == 2

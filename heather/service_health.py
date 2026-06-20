"""
heather.service_health — Circuit Breaker Pattern
==================================================
Tracks service health with exponential backoff, half-open testing,
and one-shot alerting for all external services (LLM, Ollama, ComfyUI, TTS).

Replaces: heather_telegram_bot.py lines 2998-3072
  - ServiceHealth class: lines 2998-3066
  - Health tracker instances: lines 3069-3072

Dependencies: heather.logging_setup (main_logger)
Used by: heather.text_pipeline.llm_client, heather.voice, heather.media_generation,
         heather.image_analysis, heather.monitoring
"""

from __future__ import annotations

import time
from typing import Dict

from heather.logging_setup import main_logger


class ServiceHealth:
    """Track service health for circuit breaker pattern.

    Circuit states:
      - CLOSED: service is healthy, all requests pass through
      - OPEN: service is failing, requests are blocked
      - HALF-OPEN: recovery time has passed, one test request allowed

    Features:
      - Exponential backoff on repeated failures (2x per cycle, capped at 10x base)
      - Auto-reset backoff after 30 min of prolonged outage
      - Half-open latch prevents multiple concurrent test requests
      - One-shot alert flag for new outages
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        recovery_time: int = 60,
    ) -> None:
        self.name: str = name
        self.failure_threshold: int = failure_threshold
        self.recovery_time: int = recovery_time
        self._base_recovery_time: int = recovery_time
        self._max_recovery_time: int = recovery_time * 10
        self._max_outage_before_reset: int = 1800  # 30 min
        self._outage_start_time: float = 0.0
        self.consecutive_failures: int = 0
        self.last_failure_time: float = 0.0
        self.circuit_open: bool = False
        self._half_open_testing: bool = False
        self._pending_alert: bool = False

    def record_success(self) -> None:
        """Record a successful request. Resets circuit to CLOSED."""
        self.consecutive_failures = 0
        self.circuit_open = False
        self._half_open_testing = False
        self._outage_start_time = 0.0
        self.recovery_time = self._base_recovery_time

    def record_failure(self) -> None:
        """Record a failed request. Opens circuit after threshold failures."""
        self.consecutive_failures += 1
        self.last_failure_time = time.time()
        self._half_open_testing = False

        was_open = self.circuit_open
        if self.consecutive_failures >= self.failure_threshold:
            self.circuit_open = True

            if not was_open:
                self._outage_start_time = time.time()
                self._pending_alert = True

            # Exponential backoff on repeated failures
            if was_open:
                self.recovery_time = min(
                    self.recovery_time * 2, self._max_recovery_time
                )

            # Auto-reset backoff after prolonged outage
            if self._outage_start_time and (
                time.time() - self._outage_start_time > self._max_outage_before_reset
            ):
                self.recovery_time = self._base_recovery_time
                self._outage_start_time = time.time()
                main_logger.info(
                    f"Circuit breaker {self.name}: backoff reset to "
                    f"{self.recovery_time}s after prolonged outage"
                )

            main_logger.warning(
                f"Circuit breaker OPEN for {self.name} after "
                f"{self.consecutive_failures} failures "
                f"(next retry in {self.recovery_time}s)"
            )

    def needs_alert(self) -> bool:
        """Check if a new-outage alert should be sent (one-shot).

        Returns:
            True exactly once when a circuit first opens, then False
            until the next new outage.
        """
        if self._pending_alert:
            self._pending_alert = False
            return True
        return False

    def is_available(self) -> bool:
        """Check if the service is available for requests.

        Returns:
            True if circuit is CLOSED, or if circuit is OPEN but recovery
            time has passed and no other half-open test is in progress.
        """
        if not self.circuit_open:
            return True

        if time.time() - self.last_failure_time > self.recovery_time:
            # Only let ONE test request through at a time
            if self._half_open_testing:
                return False
            self._half_open_testing = True
            main_logger.info(
                f"Circuit breaker HALF-OPEN for {self.name}, allowing test request"
            )
            return True

        return False

    def get_status(self) -> str:
        """Human-readable status string for monitoring dashboard."""
        if self.circuit_open:
            return (
                f"CIRCUIT OPEN ({self.consecutive_failures} failures, "
                f"retry in {self.recovery_time}s)"
            )
        return "OK"


# ── Pre-built Health Tracker Instances ────────────────────────────────

text_ai_health: ServiceHealth = ServiceHealth(
    "TEXT_AI", failure_threshold=5, recovery_time=30
)
ollama_health: ServiceHealth = ServiceHealth(
    "OLLAMA", failure_threshold=8, recovery_time=300
)
comfyui_health: ServiceHealth = ServiceHealth(
    "COMFYUI", failure_threshold=3, recovery_time=120
)
tts_health: ServiceHealth = ServiceHealth(
    "TTS", failure_threshold=5, recovery_time=60
)

# Lookup by service name
health_trackers: Dict[str, ServiceHealth] = {
    "TEXT_AI": text_ai_health,
    "OLLAMA": ollama_health,
    "COMFYUI": comfyui_health,
    "TTS": tts_health,
}


# ============================================================================
# Unit test stubs
# ============================================================================
# def test_circuit_starts_closed():
#     h = ServiceHealth('TEST', failure_threshold=3, recovery_time=10)
#     assert h.is_available() is True
#     assert h.circuit_open is False
#
# def test_circuit_opens_after_threshold():
#     h = ServiceHealth('TEST', failure_threshold=3, recovery_time=10)
#     for _ in range(3):
#         h.record_failure()
#     assert h.circuit_open is True
#     assert h.is_available() is False
#
# def test_circuit_resets_on_success():
#     h = ServiceHealth('TEST', failure_threshold=2, recovery_time=10)
#     h.record_failure()
#     h.record_failure()
#     assert h.circuit_open is True
#     h.record_success()
#     assert h.circuit_open is False
#     assert h.consecutive_failures == 0
#
# def test_needs_alert_one_shot():
#     h = ServiceHealth('TEST', failure_threshold=1, recovery_time=10)
#     h.record_failure()
#     assert h.needs_alert() is True
#     assert h.needs_alert() is False  # Only fires once
#
# def test_exponential_backoff():
#     h = ServiceHealth('TEST', failure_threshold=1, recovery_time=10)
#     h.record_failure()  # Opens circuit, recovery_time stays 10
#     h.record_failure()  # Already open, doubles to 20
#     assert h.recovery_time == 20
#     h.record_failure()  # Doubles to 40
#     assert h.recovery_time == 40
#
# def test_backoff_capped_at_max():
#     h = ServiceHealth('TEST', failure_threshold=1, recovery_time=10)
#     for _ in range(20):
#         h.record_failure()
#     assert h.recovery_time <= 100  # 10 * 10 = max
#
# def test_health_trackers_dict():
#     assert 'TEXT_AI' in health_trackers
#     assert 'TTS' in health_trackers
#     assert health_trackers['TEXT_AI'] is text_ai_health

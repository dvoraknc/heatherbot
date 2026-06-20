"""
heather.logging_setup — Centralized Multi-Service Logging
==========================================================
Logger factory, error routing, performance timing, and structured
context injection for all HeatherBot subsystems.

Replaces: heather_telegram_bot.py lines 101-192
  - setup_logger() function: lines 108-137
  - Logger instances: lines 140-148
  - log_error(): lines 150-166
  - log_performance(): lines 168-171
  - PerformanceTimer class: lines 173-192

Dependencies: heather.config (LOG_DIR, DEBUG_MODE)
Used by: Every heather module that logs
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, Optional

from heather import config


# ── Ensure log directory exists ────────────────────────────────────────

os.makedirs(config.LOG_DIR, exist_ok=True)


# ── Logger Factory ─────────────────────────────────────────────────────

def setup_logger(
    name: str,
    log_file: str,
    level: int = logging.INFO,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 3,
) -> logging.Logger:
    """Create a logger with rotating file handler and console output.

    Args:
        name: Logger name (e.g., 'heather_bot', 'text_ai').
        log_file: Filename within LOG_DIR.
        level: Logging level for the file handler.
        max_bytes: Max file size before rotation.
        backup_count: Number of rotated files to keep.

    Returns:
        Configured logger instance. Idempotent — returns existing logger
        if already set up.
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    if logger.handlers:
        return logger

    file_handler = RotatingFileHandler(
        os.path.join(config.LOG_DIR, log_file),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(level)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG if config.DEBUG_MODE else logging.INFO)

    detailed_formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(detailed_formatter)
    console_handler.setFormatter(detailed_formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger


# ── Pre-built Logger Instances ─────────────────────────────────────────

main_logger: logging.Logger = setup_logger(
    "heather_bot", "heather_bot.log",
    logging.DEBUG if config.DEBUG_MODE else logging.INFO,
)
text_ai_logger: logging.Logger = setup_logger("text_ai", "text_ai.log")
ollama_logger: logging.Logger = setup_logger("ollama", "ollama.log")
comfyui_logger: logging.Logger = setup_logger("comfyui", "comfyui.log")
tts_logger: logging.Logger = setup_logger("tts", "tts.log")
error_logger: logging.Logger = setup_logger("errors", "errors.log", logging.ERROR)
perf_logger: logging.Logger = setup_logger("performance", "performance.log")

# Convenience alias (matches monolith's `logger = main_logger`)
logger: logging.Logger = main_logger

# Service name -> logger mapping for log_error routing
_SERVICE_LOGGERS: Dict[str, logging.Logger] = {
    "TEXT_AI": text_ai_logger,
    "OLLAMA": ollama_logger,
    "COMFYUI": comfyui_logger,
    "TTS": tts_logger,
}


# ── Error Logging ─────────────────────────────────────────────────────

def log_error(
    service: str,
    error: str,
    context: Optional[Dict[str, Any]] = None,
) -> None:
    """Log error to both the service-specific log and the consolidated error log.

    Args:
        service: Service name (TEXT_AI, OLLAMA, COMFYUI, TTS, or any string).
        error: Error message.
        context: Optional dict of contextual data (serialized to JSON).
    """
    error_msg = f"[{service}] {error}"
    if context:
        error_msg += f" | Context: {json.dumps(context, default=str)}"
    error_logger.error(error_msg)

    service_logger = _SERVICE_LOGGERS.get(service, main_logger)
    service_logger.error(error)


# ── Performance Logging ───────────────────────────────────────────────

def log_performance(
    service: str,
    operation: str,
    duration_ms: float,
    success: bool,
    details: str = "",
) -> None:
    """Log a performance metric.

    Args:
        service: Service name.
        operation: What was being done.
        duration_ms: How long it took in milliseconds.
        success: Whether it succeeded.
        details: Optional extra context.
    """
    status = "SUCCESS" if success else "FAILED"
    perf_logger.info(f"{service} | {operation} | {duration_ms:.0f}ms | {status} | {details}")


class PerformanceTimer:
    """Context manager for timing operations and auto-logging results.

    Usage::

        with PerformanceTimer('TEXT_AI', 'generate', f'chat_id={chat_id}'):
            response = call_llm(...)
    """

    def __init__(self, service: str, operation: str, details: str = "") -> None:
        self.service = service
        self.operation = operation
        self.details = details
        self.start_time: float = 0.0
        self.success: bool = True

    def __enter__(self) -> PerformanceTimer:
        self.start_time = time.time()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        duration_ms = (time.time() - self.start_time) * 1000
        if exc_type is not None:
            self.success = False
            self.details = f"{self.details} | Error: {exc_val}"
        log_performance(self.service, self.operation, duration_ms, self.success, self.details)
        return False  # Don't suppress exceptions


@contextmanager
def pipeline_stage(stage_name: str, chat_id: int = 0):
    """Context manager for timing pipeline stages with structured logging.

    Usage::

        with pipeline_stage('safety_check', chat_id=123):
            action = safety.full_safety_check(ctx, state, tier)

    Logs: ``PIPELINE | safety_check | 12ms | SUCCESS | chat_id=123``
    """
    start = time.time()
    try:
        yield
        duration_ms = (time.time() - start) * 1000
        perf_logger.info(
            f"PIPELINE | {stage_name} | {duration_ms:.0f}ms | SUCCESS | chat_id={chat_id}"
        )
    except Exception:
        duration_ms = (time.time() - start) * 1000
        perf_logger.info(
            f"PIPELINE | {stage_name} | {duration_ms:.0f}ms | FAILED | chat_id={chat_id}"
        )
        raise


# ============================================================================
# Unit test stubs
# ============================================================================
# def test_setup_logger_idempotent():
#     """Calling setup_logger twice returns same logger, no duplicate handlers."""
#     l1 = setup_logger('test_idem', 'test.log')
#     l2 = setup_logger('test_idem', 'test.log')
#     assert l1 is l2
#     assert len(l1.handlers) == 2  # file + console
#
# def test_log_error_routes_to_service():
#     """log_error should write to both error log and service-specific log."""
#     # Would need to capture log output
#     pass
#
# def test_performance_timer_success():
#     """PerformanceTimer should log SUCCESS on clean exit."""
#     with PerformanceTimer('TEST', 'op') as t:
#         pass
#     assert t.success is True
#
# def test_performance_timer_failure():
#     """PerformanceTimer should log FAILED on exception."""
#     try:
#         with PerformanceTimer('TEST', 'op') as t:
#             raise ValueError("boom")
#     except ValueError:
#         pass
#     assert t.success is False
#
# def test_pipeline_stage():
#     """pipeline_stage should log timing for the wrapped block."""
#     with pipeline_stage('test_stage', chat_id=42):
#         pass  # Should log SUCCESS

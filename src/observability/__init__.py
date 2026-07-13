"""
Observability — centralized logging, metrics, and alerting for the paper trading system.

Provides:
  - setup_logging(): consistent structured+console logging for all modules
  - get_logger(name): structured logger with JSON extra-data support
  - metrics (MetricsRegistry): in-memory counters, gauges, histograms
  - alert (AlertManager): P0/P1/INFO alert routing
  - telegram_alert(): send P0 alerts to Telegram via webhook
  - TraderLogger: per-agent structured JSONL logging to logs/{agent_id}/YYYY-MM-DD.jsonl

Usage:
    from src.observability import setup_logging, get_logger, metrics, alert

    setup_logging(level="INFO", json_log="logs/trading.jsonl")
    logger = get_logger("my_module")
    logger.info("processing", extra={"ticker": "AAPL"})
    metrics.increment("trades.executed", tags={"trader": "kairos"})
    alert.p0("Circuit breaker tripped", {"trader": "kairos", "reason": "drawdown"})

Ref: SPEC-v3 observability requirements, issue#75
"""

from __future__ import annotations

from src.observability.logger import setup_logging, get_logger, JsonFormatter, StructuredLogAdapter
from src.observability.metrics import MetricsRegistry, metrics
from src.observability.alert import AlertManager, alert
from src.observability.telegram import telegram_alert, configure_telegram
from src.observability.trader_logger import TraderLogger, get_trader_logger

__all__ = [
    "setup_logging",
    "get_logger",
    "JsonFormatter",
    "StructuredLogAdapter",
    "MetricsRegistry",
    "metrics",
    "AlertManager",
    "alert",
    "telegram_alert",
    "configure_telegram",
    "TraderLogger",
    "get_trader_logger",
]
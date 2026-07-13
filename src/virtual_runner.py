#!/usr/bin/env python3
"""Virtual Trader Runner — runs virtual traders on paper during market hours.

Every 5 min: fetch data bus snapshot → each virtual trader decides → log to Postgres.
Also runs LIVE trader baseline for comparison (trade_source='live').

Architecture:
  For each tick (every 5 min during market):
  1. Fetch data bus snapshot from docker.klo:5000 (/quotes, /signals/momentum)
  2. For each active virtual trader in trading.virtual_traders (status='active'):
     a. Load its config overrides (JSONB from DB)
     b. Apply overrides to signal engine params
     c. Build prompt with overridden signals, portfolio, journal
     d. Call LLM (Gemini Flash via OpenRouter)
     e. Parse BUY/SELL/HOLD from LLM response
     f. Log decision to trading.trades with trade_source='virtual'
  3. Also run the LIVE trader (trade_source='live') for comparison

Usage:
    python3 src/virtual_runner.py                # run continuously during market hours
    python3 src/virtual_runner.py --once          # run one cycle and exit (for testing)
    python3 src/virtual_runner.py --virtuals kairos-looser,kairos-tighter  # specific virtuals
    python3 src/virtual_runner.py --virtuals kairos-looser --once           # test single virtual
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.signals import SignalEngine, SignalParams, SignalReport
from src.llm_engine import LLMEngine
from src.prompt_builder import PromptBuilder, AgentFiles, DEFAULTS as PROMPT_DEFAULTS
import numpy as np
from src.prompt_builder import PromptBuilder, DEFAULTS as PROMPT_DEFAULTS
from src.agent_files import AgentFiles
from src.replay import Tick, Portfolio, TraderDecision

# Agent stubs — virtual trader persona definitions
from src.agents.registry import create_trader, list_personas, get_persona, load_all_persona_configs
from src.agents.decision_schema import (
    TickContext as VTickContext,
    PortfolioSnapshot as VPortfolioSnapshot,
    VirtualDecision,
    validate_decision_list,
)

log = logging.getLogger("virtual_runner")

# ── Config (mutable dict — CLI args can override) ─────────────────────────────

_config: Dict[str, Any] = {
    "db_dsn": os.getenv("VT_DB_DSN", "host=192.168.1.179 port=5433 dbname=trading user=trader"),
    "data_bus_url": os.getenv("VT_DATA_BUS_URL", "http://192.168.1.25:5000"),
    "model": os.getenv("VT_MODEL", "google/gemini-3.5-flash"),
    "interval": 300,  # seconds (5 min)
    "max_parallel": int(os.getenv("VT_MAX_PARALLEL", "24")),
    "starting_cash": float(os.getenv("VT_STARTING_CASH", "10000")),
    "mock": int(os.getenv("VT_MOCK", "0")) == 1 or False,    # True to bypass network and generate fake data
    "live": False,    # True to submit real orders to Alpaca paper trading
}

# Market hours (ET)
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MIN = 30
MARKET_CLOSE_HOUR = 16
MARKET_CLOSE_MIN = 0


def is_market_hours() -> bool:
    """Check if we're within 09:30–16:00 ET on a weekday."""
    try:
        import pytz
        eastern = pytz.timezone("US/Eastern")
    except Exception:
        return True  # Fallback: assume open if pytz unavailable

    now = datetime.now(eastern)
    if now.weekday() >= 5:
        return False

    open_time = now.replace(
        hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MIN, second=0, microsecond=0
    )
    close_time = now.replace(
        hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MIN, second=0, microsecond=0
    )
    return open_time <= now <= close_time


# ── Data Bus Client ───────────────────────────────────────────────────────────


def fetch_quotes(symbols: List[str]) -> Dict[str, dict]:
    """Fetch live quotes from the data bus."""
    if _config.get("mock"):
        return {
            symbol: {
                "open": 150.0,
                "high": 152.0,
                "low": 149.0,
                "price": 151.0,
                "volume": 1000000,
                "rsi": 50.0,
                "momentum": 0.05,
                "volatility": 0.15,
                "regime": "bull_quiet",
            }
            for symbol in symbols
        }

    if not symbols:
        return {}

    url = f"{_config['data_bus_url']}/quotes?symbols={','.join(symbols)}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return data.get("quotes", {})
    except Exception as e:
        log.error("Failed to fetch quotes: %s", e)
        return {}


def fetch_momentum_signals(symbols: List[str]) -> Dict[str, dict]:
    """Fetch pre-computed momentum signals from the data bus."""
    if _config.get("mock"):
        return {
            symbol: {
                "rsi": 52.0,
                "momentum": 0.04,
                "regime": "bull_quiet",
            }
            for symbol in symbols
        }

    if not symbols:
        return {}

    url = f"{_config['data_bus_url']}/signals/momentum?symbols={','.join(symbols)}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return data.get("signals", {})
    except Exception as e:
        log.debug("Could not fetch momentum signals (non-critical): %s", e)
        return {}


def get_tracked_symbols() -> List[str]:
    """Get list of tracked symbols from the data bus health endpoint."""
    if _config.get("mock"):
        return ["SPY", "AAPL", "NVDA", "MSFT"]
    try:
        with urllib.request.urlopen(f"{_config['data_bus_url']}/health", timeout=5) as resp:
            data = json.loads(resp.read().decode())
            entries = data.get("cache_stats", {}).get("entries", [])
            symbols = []
            for entry in entries:
                if entry.startswith("quote:"):
                    symbols.append(entry.split(":", 1)[1])
            return symbols if symbols else ["SPY", "AAPL", "NVDA", "MSFT"]
    except Exception as e:
        log.warning("Could not get tracked symbols: %s", e)
        return ["SPY", "AAPL", "NVDA", "MSFT", "GOOGL", "META", "TSLA"]


# ── Database ──────────────────────────────────────────────────────────────────


def get_db():
    """Return a sync psycopg2 connection."""
    conn = psycopg2.connect(_config["db_dsn"])
    conn.autocommit = True
    return conn


def load_virtual_traders(names: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Load active virtual traders from the database.

    Args:
        names: If provided, only load virtuals whose name is in this list.
               Otherwise load all active virtuals.

    Returns:
        List of virtual trader rows with id, name, base_trader, variant_type,
        config, and status.
    """
    if _config.get("mock"):
        # Original mock traders (legacy)
        all_vt = [
            {
                "id": "vt-mock-1",
                "name": "kairos-looser",
                "base_trader": "kairos",
                "variant_type": "looser",
                "config": {"rsi_period": 14},
                "status": "active"
            },
            {
                "id": "vt-mock-2",
                "name": "trader-kairos",
                "base_trader": "kairos",
                "variant_type": "base",
                "config": {},
                "status": "active"
            },
        ]

        # Add persona-based virtual traders (Phase 1 — agent stubs)
        persona_configs = load_all_persona_configs()
        for pid, pconfig in persona_configs.items():
            persona_instance = create_trader(
                persona_id=pid,
                trader_name=f"vt-{pid}-001",
                starting_cash=_config["starting_cash"],
                data_bus_url=_config["data_bus_url"],
            )
            all_vt.append({
                "id": f"vt-persona-{pid}",
                "name": f"vt-{pid}-001",
                "base_trader": pconfig.get("base_trader") or pid,
                "variant_type": pid,
                "config": pconfig.get("default_config", {}),
                "status": "active",
                "is_persona": True,
                "persona_id": pid,
                "trader_instance": persona_instance,
            })

        if names:
            return [v for v in all_vt if v["name"] in names]
        return all_vt

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if names:
        # Build safe parameterized query for specific names
        placeholders = ",".join(["%s"] * len(names))
        cur.execute(
            f"SELECT id, name, base_trader, variant_type, config, status "
            f"FROM trading.virtual_traders WHERE status = 'active' AND name IN ({placeholders}) "
            f"ORDER BY base_trader, name",
            names,
        )
    else:
        cur.execute(
            "SELECT id, name, base_trader, variant_type, config, status "
            "FROM trading.virtual_traders WHERE status = 'active' "
            "ORDER BY base_trader, name"
        )
    rows = cur.fetchall()
    conn.close()
    return rows


def _get_alpaca_trading_client(base_trader: str):
    """Get an Alpaca TradingClient for the given base trader (paper).

    Returns None if keys are unavailable.
    """
    try:
        from alpaca.trading.client import TradingClient
        key_env, secret_env = _ALPACA_KEYS.get(base_trader, (None, None))
        if not key_env:
            return None
        from dotenv import load_dotenv
        load_dotenv(Path.home() / ".openclaw" / ".env", override=True)
        api_key = os.getenv(key_env)
        secret = os.getenv(secret_env)
        if not api_key or not secret:
            return None
        return TradingClient(api_key, secret, paper=True)
    except Exception as e:
        log.warning("Could not create Alpaca TradingClient: %s", e)
        return None


def _submit_alpaca_order(
    client: "TradingClient",
    ticker: str,
    decision: str,
    shares: int,
    trader_id: str,
) -> Optional[str]:
    """Submit a market order to Alpaca paper trading.

    Returns the Alpaca order ID on success, None on failure.
    """
    try:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        side = OrderSide.BUY if decision == "BUY" else OrderSide.SELL
        order_data = MarketOrderRequest(
            symbol=ticker,
            qty=shares if shares > 0 else 1,
            side=side,
            time_in_force=TimeInForce.DAY,
        )
        order = client.submit_order(order_data)
        order_id = getattr(order, "id", str(uuid.uuid4()))
        log.info(
            "LIVE ORDER | trader=%s | ticker=%s | side=%s | shares=%d | order_id=%s",
            trader_id, ticker, decision, shares, order_id,
        )
        return str(order_id)
    except Exception as e:
        log.warning("Alpaca order failed for %s %s: %s", trader_id, ticker, e)
        return None


def insert_trade(
    trader_id: str,
    ticker: str,
    decision: str,
    conviction: float,
    rationale: str,
    price: float,
    trade_source: str,
    shares: int = 0,
    regime: Optional[str] = None,
    signal_score: Optional[float] = None,
    base_trader: Optional[str] = None,
):
    """Insert a trade decision into trading.trades.

    For BUY/SELL decisions we record the entry. P&L is computed later
    when the position is closed (by the live trader system or virtual_rotate).

    When --live mode is active, also submits a real order to Alpaca paper trading.
    """
    if _config.get("mock"):
        trade_id = f"vt-mock-{uuid.uuid4().hex[:12]}"
        log.info(
            "OFFLINE TRADE (NO DB) | trader=%s | ticker=%s | decision=%s | conv=%.2f | source=%s | signal=%.2f",
            trader_id, ticker, decision, conviction, trade_source,
            signal_score or 0.0,
        )
        return trade_id

    conn = get_db()
    cur = conn.cursor()
    trade_id = f"vt-{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)
    alpaca_order_id = None

    # If --live is active, submit real order to Alpaca
    if _config.get("live") and base_trader and decision in ("BUY", "SELL"):
        alpaca_client = _get_alpaca_trading_client(base_trader)
        if alpaca_client:
            live_shares = shares if shares > 0 else max(1, int(_config.get("starting_cash", 10000) * 0.05 / max(price, 0.01)))
            alpaca_order_id = _submit_alpaca_order(alpaca_client, ticker, decision, live_shares, trader_id)

    cur.execute(
        """INSERT INTO trading.trades
           (trader_id, trade_id, ticker, entry_time, entry_price, shares,
            pnl, return_pct, regime, trade_source, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            trader_id,
            trade_id,
            ticker,
            now,
            price,
            shares if shares > 0 else 0,
            0.0,  # P&L computed on close
            0.0,  # return_pct computed on close
            regime,
            f"{trade_source}_live" if alpaca_order_id else trade_source,
            now,
        ),
    )
    conn.close()

    if alpaca_order_id:
        log.info(
            "TRADE+LIVE | trader=%s | ticker=%s | decision=%s | conv=%.2f | source=%s | alpaca=%s",
            trader_id, ticker, decision, conviction, trade_source, alpaca_order_id,
        )
    else:
        log.info(
            "TRADE | trader=%s | ticker=%s | decision=%s | conv=%.2f | source=%s | signal=%.2f",
            trader_id, ticker, decision, conviction, trade_source,
            signal_score or 0.0,
        )
    return trade_id


# ── Portfolio State (per-virtual) ─────────────────────────────────────────────


@dataclass
class VirtualPosition:
    """An open virtual position."""
    ticker: str
    shares: int
    entry_price: float
    entry_time: datetime


@dataclass
class VirtualPortfolio:
    """In-memory portfolio state for one virtual trader."""
    cash: float = field(default_factory=lambda: _config["starting_cash"])
    positions: Dict[str, VirtualPosition] = field(default_factory=dict)

    @property
    def total_equity(self) -> float:
        return self.cash + sum(
            p.shares * p.entry_price for p in self.positions.values()
        )

    @property
    def position_count(self) -> int:
        return len(self.positions)

    def to_llm_portfolio(self) -> Portfolio:
        """Convert to a replay.Portfolio for the LLM engine."""
        llm_positions = {}
        for tkr, pos in self.positions.items():
            from src.replay import Position
            llm_positions[tkr] = Position(
                ticker=tkr,
                shares=pos.shares,
                entry_price=pos.entry_price,
                entry_time=pos.entry_time,
                current_price=pos.entry_price,  # approximate
            )
        return Portfolio(cash=self.cash, positions=llm_positions)


# Global portfolio registry (keyed by trader_name)
_portfolios: Dict[str, VirtualPortfolio] = {}

# Live equity cache — refreshed each cycle so new virtuals start at real equity
_live_equities: Dict[str, float] = {}

# Alpaca key mapping — mirrors the live trader accounts
_ALPACA_KEYS = {
    "kairos":   ("KAIROS_API_KEY",   "KAIROS_SECRET_KEY"),
    "aldridge": ("ALDRIDGE_API_KEY", "ALDRIDGE_SECRET_KEY"),
    "stonks":   ("STONKS_API_KEY",   "STONKS_SECRET_KEY"),
}


def _fetch_live_equity(base_trader: str) -> float:
    """Fetch current paper account equity from Alpaca for a base trader.

    Falls back to config starting_cash if Alpaca is unreachable.
    """
    try:
        from dotenv import load_dotenv
        from alpaca.trading.client import TradingClient

        load_dotenv(Path.home() / ".openclaw" / ".env", override=True)
        key_env, secret_env = _ALPACA_KEYS.get(base_trader, (None, None))
        if not key_env:
            raise ValueError(f"No Alpaca keys for base trader: {base_trader}")

        client = TradingClient(os.getenv(key_env), os.getenv(secret_env), paper=True)
        account = client.get_account()
        return float(account.equity)
    except Exception as e:
        log.warning("Could not fetch live equity for %s: %s — using default $%s",
                      base_trader, e, _config["starting_cash"])
        return _config["starting_cash"]


def refresh_live_equities():
    """Refresh live equity cache for all base traders."""
    global _live_equities
    for base in _ALPACA_KEYS:
        _live_equities[base] = _fetch_live_equity(base)
    log.info("Live equities: %s", {k: f"${v:,.2f}" for k, v in _live_equities.items()})


def get_portfolio(trader_name: str, base_trader: str = None, is_live: bool = False) -> VirtualPortfolio:
    """Get or create a virtual portfolio for a trader.

    For virtual traders, starting cash = their base trader's current Alpaca equity.
    For live baselines, use the Alpaca equity directly.
    New portfolios always inherit the latest live equity (not fixed $10K).
    """
    if trader_name not in _portfolios:
        cash = _config["starting_cash"]
        if base_trader and base_trader in _live_equities:
            cash = _live_equities[base_trader]
        _portfolios[trader_name] = VirtualPortfolio(cash=cash)
    return _portfolios[trader_name]


def reset_portfolios():
    """Reset all virtual portfolios (for testing/new day)."""
    _portfolios.clear()


# ── Signal + LLM Pipeline ─────────────────────────────────────────────────────


def quotes_to_ticks(
    quotes: Dict[str, dict],
    signals: Optional[Dict[str, dict]] = None,
) -> List[Tick]:
    """Convert data bus quotes to Tick objects, merging pre-computed signals."""
    ticks = []
    now = datetime.now(timezone.utc)
    for symbol, q in quotes.items():
        sig = signals.get(symbol, {}) if signals else {}
        tick = Tick(
            timestamp=now,
            ticker=symbol,
            open=q.get("open", q.get("price", 0)),
            high=q.get("high", q.get("price", 0)),
            low=q.get("low", q.get("price", 0)),
            close=q.get("price", 0),
            volume=q.get("volume", 0),
            rsi=q.get("rsi", sig.get("rsi")),
            momentum=q.get("momentum", sig.get("momentum")),
            volatility=q.get("volatility"),
            regime=q.get("regime", sig.get("regime")),
        )
        ticks.append(tick)
    return ticks


# ── Signal pre-warming ──────────────────────────────────────────────────────

def fetch_bars_for_ticks(symbols: List[str], days: int = 60) -> Dict[str, List[dict]]:
    """Fetch daily bars from data bus to pre-warm signal engines."""
    if not symbols:
        return {}
    try:
        from datetime import datetime as dt, timedelta
        end = dt.now().strftime("%Y-%m-%d")
        start = (dt.now() - timedelta(days=days + 5)).strftime("%Y-%m-%d")
        url = f"{_config['data_bus_url']}/bars?symbols={','.join(symbols)}&interval=daily&start_date={start}&end_date={end}"
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            return data.get("symbols", {})
    except Exception as e:
        log.debug("Could not fetch bars for signal pre-warm: %s", e)
        return {}


def prewarm_signal_engine(
    engine: SignalEngine,
    bars: Dict[str, List[dict]],
    ticker: str,
) -> None:
    """Feed historical bars into the signal engine so indicators have data."""
    ticker_bars = bars.get(ticker, [])
    if not ticker_bars:
        return
    from src.replay import Tick as _Tick
    for bar in ticker_bars:
        engine._price_history.setdefault(ticker, []).append(float(bar["close"]))
        engine._volume_history.setdefault(ticker, []).append(int(bar.get("volume", 0)))
        # Trim to max_history
        if len(engine._price_history[ticker]) > engine.max_history:
            engine._price_history[ticker].pop(0)
        if len(engine._volume_history[ticker]) > engine.max_history:
            engine._volume_history[ticker].pop(0)


def compute_rsi_from_prices(prices: List[float], lookback: int = 14) -> float:
    """Compute RSI from a list of prices."""
    if len(prices) < lookback + 1:
        return 50.0
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[-lookback:])
    avg_loss = np.mean(losses[-lookback:])
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def compute_momentum_from_prices(prices: List[float], lookback: int = 21) -> float:
    """Compute simple rate of change momentum."""
    if len(prices) < lookback + 1:
        # Use whatever we have
        lookback = max(len(prices) - 1, 1)
    if lookback >= len(prices):
        return 0.0
    return (prices[-1] - prices[-lookback - 1]) / prices[-lookback - 1] * 100


def attach_bar_signals_to_ticks(
    ticks: List[Tick],
    bars: Dict[str, List[dict]],
) -> None:
    """Pre-compute RSI/momentum/regime from bars and attach to Tick objects."""
    for tick in ticks:
        ticker_bars = bars.get(tick.ticker, [])
        if len(ticker_bars) < 5:
            continue
        closes = [b["close"] for b in ticker_bars]
        # RSI
        if tick.rsi is None:
            tick.rsi = round(compute_rsi_from_prices(closes), 1)
        # Momentum
        if tick.momentum is None:
            tick.momentum = round(compute_momentum_from_prices(closes), 2)
        # Volatility
        if tick.volatility is None and len(closes) > 10:
            returns = np.diff(closes) / closes[:-1]
            tick.volatility = round(float(np.std(returns[-20:])) * 100, 2)
        # Simple regime
        if tick.regime is None and len(closes) > 20:
            short_ma = np.mean(closes[-5:])
            long_ma = np.mean(closes[-20:])
            if short_ma > long_ma * 1.02:
                tick.regime = "TRENDING_UP"
            elif short_ma < long_ma * 0.98:
                tick.regime = "TRENDING_DOWN"
            else:
                tick.regime = "MEAN_REVERTING"


# ── Ticker diversity ─────────────────────────────────────────────────────────

SECTOR_MAP = {
    "AAPL": "Tech", "MSFT": "Tech", "GOOGL": "Tech", "META": "Tech",
    "AMZN": "Consumer", "TSLA": "Auto", "NVDA": "Semiconductors",
    "SPY": "ETF", "QQQ": "ETF",
    "JPM": "Financial", "BAC": "Financial", "V": "Financial",
    "WMT": "Consumer", "DIS": "Entertainment", "KO": "Consumer",
    "JNJ": "Healthcare", "XOM": "Energy",
}


def diversify_tickers(symbols: List[str], max_per_sector: int = 3) -> List[str]:
    """Limit to max_per_sector per sector to ensure variety."""
    sector_count: Dict[str, int] = {}
    result = []
    for sym in symbols:
        sector = SECTOR_MAP.get(sym, "Other")
        if sector_count.get(sector, 0) < max_per_sector:
            result.append(sym)
            sector_count[sector] = sector_count.get(sector, 0) + 1
    return result if result else symbols


# ── Signal params builder ────────────────────────────────────────────────────

def build_signal_params(
    base: str, virtual_config: Optional[Dict[str, Any]] = None
) -> SignalParams:
    """Create SignalParams, optionally overriding with virtual trader config."""
    params = SignalParams()

    if virtual_config:
        for key, value in virtual_config.items():
            if hasattr(params, key):
                try:
                    params.set(key, float(value))
                except (ValueError, TypeError):
                    log.debug("Skipping non-numeric config key: %s=%s", key, value)

    return params


def pick_best_ticker(
    ticks: List[Tick],
    signal_params: SignalParams,
    trader_name: str = "",
    bars: Optional[Dict[str, List[dict]]] = None,
) -> tuple[Optional[Tick], Optional[SignalReport]]:
    """Compute signals for all tickers and pick the one with highest |composite_signal|.

    Pre-warms the signal engine with historical bars so momentum, RSI, and regime
    detection have real data instead of returning 0.0 on the first tick.
    """
    signal_engine = SignalEngine(params=signal_params, max_history=252)

    # Pre-warm from historical bars
    if bars:
        for tick in ticks:
            prewarm_signal_engine(signal_engine, bars, tick.ticker)

    best_ticker = None
    best_signal = None
    best_abs_score = -1.0

    for tick in ticks:
        try:
            # Bootstrap: virtual runner starts fresh every cycle,
            # so volume filter bypass helps make initial entries.
            bootstrap = len(get_portfolio(trader_name).positions) == 0 if trader_name else True
            signal = signal_engine.process(tick, bootstrap=bootstrap)
            abs_score = abs(signal.composite_signal)
            log.debug("  Signal for %s: composite=%.4f regime=%s rsi=%.1f bootstrap=%s",
                       tick.ticker, signal.composite_signal, signal.regime,
                       signal.rsi, bootstrap)
            if abs_score > best_abs_score:
                best_abs_score = abs_score
                best_ticker = tick
                best_signal = signal
        except Exception as e:
            log.debug("Signal error for ticker=%s: %s", tick.ticker, e)

    if best_signal and best_signal.composite_signal == 0.0:
        log.warning("  Signal engine returned 0.0 for ALL %d tickers — this means "
                    "indicators have no conviction. Check pre-warming and params.", len(ticks))

    return best_ticker, best_signal


def run_one_trader(
    trader_name: str,
    base_trader: str,
    config: Dict[str, Any],
    ticks: List[Tick],
    engine: LLMEngine,
    agent_files: AgentFiles,
    trade_source: str,
) -> Optional[Dict[str, Any]]:
    """Run one trader (virtual or live) — picks best ticker and makes ONE decision.

    Args:
        trader_name: e.g. 'kairos-looser' or 'trader-kairos'
        base_trader: e.g. 'kairos'
        config: overrides dict from DB (empty for live)
        ticks: all available ticks
        engine: LLMEngine instance (shared across threads)
        agent_files: pre-loaded AgentFiles
        trade_source: 'virtual' or 'live'

    Returns:
        Decision dict or None if no actionable signal.
    """
    # Compute signals
    signal_params = build_signal_params(base_trader, config)
    best_ticker, best_signal = pick_best_ticker(ticks, signal_params, trader_name=trader_name)

    if best_ticker is None or best_signal is None:
        log.debug("  %-24s no signal", trader_name)
        return None

    try:
        # Get portfolio state for this trader — inherit live equity for virtuals
        virtual_portfolio = get_portfolio(trader_name, base_trader=base_trader,
                                          is_live=(trade_source == "live"))
        portfolio = virtual_portfolio.to_llm_portfolio()

        # Call LLM
        llm_decision = engine.decide(
            tick=best_ticker,
            signal=best_signal,
            journal=[],  # Virtual runners don't have journal context yet
            portfolio=portfolio,
            agent_files=agent_files,
        )

        # Log to database (only for BUY/SELL)
        if llm_decision.decision in ("BUY", "SELL"):
            insert_trade(
                trader_id=trader_name,
                ticker=best_ticker.ticker,
                decision=llm_decision.decision,
                conviction=llm_decision.conviction,
                rationale=llm_decision.rationale,
                price=best_ticker.close,
                trade_source=trade_source,
                regime=best_signal.regime,
                signal_score=best_signal.composite_signal,
                base_trader=base_trader,
            )

        result = {
            "trader": trader_name,
            "base": base_trader,
            "ticker": best_ticker.ticker,
            "decision": llm_decision.decision,
            "conviction": llm_decision.conviction,
            "rationale": llm_decision.rationale,
            "price": best_ticker.close,
            "regime": best_signal.regime,
            "composite_signal": best_signal.composite_signal,
            "source": trade_source,
        }

        log.info(
            "  %-24s %s:%-6s conv=%s signal=%s regime=%s source=%s",
            trader_name, result["ticker"], result["decision"],
            f"{result['conviction']:.2f}" if result["conviction"] is not None else "N/A",
            f"{result['composite_signal']:+.2f}" if result["composite_signal"] is not None else "N/A",
            result["regime"] or "N/A",
            result["source"],
        )
        return result

    except Exception as e:
        log.error("  %-24s ERROR: %s", trader_name, e, exc_info=True)
        return None


# ── Persona-Based Trader Runner ───────────────────────────────────────────────


def run_one_persona_trader(
    trader_instance: Any,
    tick_context: VTickContext,
) -> Optional[Dict[str, Any]]:
    """Run one persona-based virtual trader using its evaluate() method.

    Instead of calling the LLM, this uses the trader's rule-based or
    ML-based evaluate() logic. Returns a decision dict in the same
    format as run_one_trader() for compatibility with the orchestrator.

    Args:
        trader_instance: VirtualTrader subclass instance.
        tick_context: TickContext with quotes, signals, and portfolio.

    Returns:
        Decision dict (with 'decision' field) or None.
    """
    try:
        tick_decisions = trader_instance.run_tick(tick_context)
        actionables = [
            d for d in tick_decisions.decisions
            if d.action.value in ("buy", "sell")
        ]

        if not actionables:
            log.debug("  %-24s persona no actionable signal", trader_instance.trader_name)
            return None

        # Pick highest-conviction decision as the "one" decision (legacy compat)
        best = max(actionables, key=lambda d: d.conviction)

        return {
            "trader": trader_instance.trader_name,
            "base": trader_instance.persona_id(),
            "ticker": best.symbol,
            "decision": best.action.value.upper(),
            "conviction": best.conviction,
            "rationale": best.reasoning[:200],
            "price": 0.0,
            "regime": best.metadata.get("regime", "unknown"),
            "composite_signal": best.conviction,
            "source": "virtual",
            "all_decisions": [d.to_dict() for d in tick_decisions.decisions],
            "portfolio": tick_decisions.portfolio_snapshot.to_dict(),
        }
    except Exception as e:
        log.error("  persona %-24s ERROR: %s", trader_instance.trader_name, e, exc_info=True)
        return None


# ── Main Runner ───────────────────────────────────────────────────────────────


def run_once(
    engine: Optional[LLMEngine] = None,
    virtual_names: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Run one tick cycle: fetch quotes, run all virtuals + live, return summary.

    Uses ThreadPoolExecutor for parallel LLM calls with configurable concurrency
    limit. If errors occur, the cycle continues for remaining traders.

    Args:
        engine: LLMEngine instance (creates one if None).
        virtual_names: Specific virtual trader names to run. If None, runs all active.

    Returns:
        Summary dict with status, counts, and per-trader results.
    """
    if engine is None:
        engine = LLMEngine(
            model=_config["model"],
            temperature=0.3,
            max_tokens=2000,
            max_retries=2,
        )

    # 1. Fetch market data
    symbols = get_tracked_symbols()
    quotes = fetch_quotes(symbols)
    if not quotes:
        log.warning("No quotes available from data bus — skipping cycle")
        return {"status": "no_data", "virtuals": 0, "decisions": 0, "results": []}

    # Also fetch pre-computed signals (non-critical)
    momentum_signals = fetch_momentum_signals(symbols)
    ticks = quotes_to_ticks(quotes, momentum_signals)
    log.info("Fetched %d quotes (+ %d signal sets) → %d ticks",
             len(quotes), len(momentum_signals), len(ticks))

    # 2. Refresh live equity cache — virtuals inherit parent trader's balance
    refresh_live_equities()

    # 3. Load virtual traders
    virtuals = load_virtual_traders(names=virtual_names)
    if virtual_names:
        found = {v["name"] for v in virtuals}
        missing = set(virtual_names) - found
        if missing:
            log.warning("Virtuals not found/not active: %s", sorted(missing))
    log.info("Running %d virtual traders", len(virtuals))

    # 3. Build task list: virtuals + live baselines
    tasks: List[Dict[str, Any]] = []

    # Virtual traders
    for vt in virtuals:
        tasks.append({
            "trader_name": vt["name"],
            "base_trader": vt["base_trader"],
            "config": vt.get("config", {}),
            "agent_files": PROMPT_DEFAULTS.get(vt["base_trader"], PROMPT_DEFAULTS["kairos"]),
            "trade_source": "virtual",
        })

    # Live baselines (one per base trader with virtuals, or all if no virtual names specified)
    base_traders_seen = {t["base_trader"] for t in tasks}
    for base_trader in PROMPT_DEFAULTS:
        if base_trader in base_traders_seen:
            tasks.append({
                "trader_name": f"trader-{base_trader}",
                "base_trader": base_trader,
                "config": {},
                "agent_files": PROMPT_DEFAULTS[base_trader],
                "trade_source": "live",
            })

    # 4. Run all tasks in parallel with rate limiting
    max_workers = min(_config["max_parallel"], len(tasks))
    results: List[Dict[str, Any]] = []
    active_decisions = 0

    if not tasks:
        log.info("No tasks to run.")
        return {
            "status": "ok",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "virtuals_loaded": len(virtuals),
            "tasks_queued": 0,
            "decisions": 0,
            "symbols": len(symbols),
            "quotes": len(quotes),
            "results": [],
        }

    # Use ThreadPoolExecutor for parallel LLM calls
    # Each task uses a shared engine — llm_engine.decide() is thread-safe
    # since it creates its own HTTP connection per call.
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_task = {
            executor.submit(
                run_one_trader,
                trader_name=t["trader_name"],
                base_trader=t["base_trader"],
                config=t["config"],
                ticks=ticks,
                engine=engine,
                agent_files=t["agent_files"],
                trade_source=t["trade_source"],
            ): t
            for t in tasks
        }

        for future in as_completed(future_to_task):
            task = future_to_task[future]
            try:
                result = future.result()
                if result:
                    results.append(result)
                    active_decisions += 1
            except Exception as e:
                log.error(
                    "Task failed for %s (source=%s): %s",
                    task["trader_name"], task["trade_source"], e,
                )

    # Summary
    summary = {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "virtuals_loaded": len(virtuals),
        "tasks_queued": len(tasks),
        "decisions": active_decisions,
        "symbols": len(symbols),
        "quotes": len(quotes),
        "results": results,
    }

    # Log summary
    by_source = {"virtual": 0, "live": 0}
    for r in results:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
    log.info(
        "Cycle complete: %d virtual decisions + %d live decisions across %d tasks",
        by_source["virtual"], by_source["live"], len(tasks),
    )

    # ── Learning loop: run post-tick analysis for base traders ─────
    try:
        from src.learning_loop import run_for_agent, get_agents
        agents = get_agents()
        learning_results = []
        for agent_id in agents:
            try:
                lr = run_for_agent(agent_id)
                learning_results.append(lr)
                log.info("Learning loop: %s — %d trades, $%.2f P&L, %.0f%% WR — %d signals",
                         agent_id, lr["trades_count"], lr["total_pnl"],
                         lr["win_rate"], len(lr["signals"]))
            except Exception as e:
                log.warning("Learning loop failed for %s: %s", agent_id, e)
        summary["learning_loop"] = learning_results
    except ImportError:
        log.debug("Learning loop module not available — skipping post-tick analysis")
    except Exception as e:
        log.warning("Learning loop cycle failed: %s", e)

    return summary


# ── Helper: table formatter ───────────────────────────────────────────────────


def print_result_table(results: List[Dict[str, Any]]):
    """Print results as a formatted table."""
    if not results:
        print("No decisions made.")
        return

    print(f"\n{'Trader':<26} {'Source':<8} {'Ticker':<7} {'Decision':<8} "
          f"{'Conv':<6} {'Signal':<8} {'Regime'}")
    print("-" * 95)
    for r in sorted(results, key=lambda r: (r["source"], r["trader"])):
        conv = f"{r['conviction']:<5.2f}" if r['conviction'] is not None else "N/A  "
        sig  = f"{r['composite_signal']:+6.2f}" if r['composite_signal'] is not None else "   N/A"
        reg  = r['regime'] if r['regime'] else "N/A"
        print(
            f"{r['trader']:<26} {r['source']:<8} {r['ticker']:<7} "
            f"{r['decision']:<8} {conv}  "
            f"{sig}   {reg}"
        )


# ── CLI ───────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Virtual Trader Runner")
    parser.add_argument(
        "--once", action="store_true",
        help="Run one cycle and exit"
    )
    parser.add_argument(
        "--mock", action="store_true",
        help="Run in mock/offline mode with mock data and no DB connection"
    )
    parser.add_argument(
        "--virtuals", type=str, default=None,
        help="Comma-separated list of virtual trader names to run "
             "(default: all active from DB)"
    )
    parser.add_argument(
        "--interval", type=int, default=_config["interval"],
        help=f"Tick interval in seconds (default: {_config['interval']})"
    )
    parser.add_argument(
        "--model", type=str, default=_config["model"],
        help=f"LLM model to use (default: {_config['model']})"
    )
    parser.add_argument(
        "--parallel", type=int, default=_config["max_parallel"],
        help=f"Max parallel LLM calls (default: {_config['max_parallel']})"
    )
    parser.add_argument(
        "--db-dsn", type=str, default=_config["db_dsn"],
        help="Postgres connection string"
    )
    parser.add_argument(
        "--data-bus", type=str, default=_config["data_bus_url"],
        help="Data bus base URL"
    )
    parser.add_argument(
        "--starting-cash", type=float, default=_config["starting_cash"],
        help=f"Starting cash per virtual trader (default: ${_config['starting_cash']:,.0f})"
    )
    parser.add_argument(
        "--live", action="store_true",
        help="Submit real orders to Alpaca paper trading instead of just logging decisions"
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress logging, just print result table"
    )
    args = parser.parse_args()

    # Apply CLI overrides to runtime config
    _config.update({
        "db_dsn": args.db_dsn,
        "data_bus_url": args.data_bus,
        "model": args.model,
        "interval": args.interval,
        "max_parallel": args.parallel,
        "starting_cash": args.starting_cash,
        "mock": args.mock or _config.get("mock", False),
        "live": args.live or _config.get("live", False),
    })

    # Logging setup
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Parse --virtuals
    virtual_names: Optional[List[str]] = None
    if args.virtuals:
        virtual_names = [n.strip() for n in args.virtuals.split(",") if n.strip()]
        if not virtual_names:
            parser.error("--virtuals must contain at least one name")

    log.info("Virtual Trader Runner starting")
    log.info("  Model: %s", args.model)
    log.info("  Interval: %ds", args.interval)
    log.info("  Data bus: %s", args.data_bus)
    log.info("  DB: %s", args.db_dsn)
    log.info("  Parallel: %d max", args.parallel)
    log.info("  Starting cash: $%s", f"{args.starting_cash:,.0f}")
    if virtual_names:
        log.info("  Filtering to virtuals: %s", virtual_names)

    engine = LLMEngine(
        model=args.model,
        temperature=0.3,
        max_tokens=2000,
        max_retries=2,
    )

    if args.once:
        # Reset portfolios for clean test state
        reset_portfolios()
        result = run_once(engine=engine, virtual_names=virtual_names)
        if result.get("results"):
            print_result_table(result["results"])
        log.info("One-shot complete: %s tasks → %d decisions",
                 result.get("tasks_queued", 0), result.get("decisions", 0))
        return

    # Continuous loop during market hours
    cycle = 0
    while True:
        if is_market_hours():
            cycle += 1
            try:
                result = run_once(engine=engine, virtual_names=virtual_names)
                log.info(
                    "Cycle #%d: %d decisions from %d tasks (quotes=%d)",
                    cycle,
                    result.get("decisions", 0),
                    result.get("tasks_queued", 0),
                    result.get("quotes", 0),
                )
            except Exception as e:
                log.error("Cycle #%d failed: %s", cycle, e)
        else:
            log.debug("Outside market hours — sleeping")

        time.sleep(args.interval)


if __name__ == "__main__":
    main()

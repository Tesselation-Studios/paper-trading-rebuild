"""Momentum Trader Persona — rides trends and acts decisively.

Strategy:
- Buy when momentum signal is strong (>0.4) and RSI is not overbought (<70)
- Sell when momentum turns negative or RSI exceeds 80
- Volume confirmation: only enter when volume >= 1.2x rolling average
- Max 20% of portfolio in one position
- Aggressive sizing: scales into winners, cuts losers quickly

This persona corresponds to the "Kairos" base trader strategy but
with tuned parameters for more aggressive momentum capture.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.agents.base import VirtualTrader
from src.agents.decision_schema import (
    Action,
    OrderType,
    TickContext,
    TimeInForce,
    VirtualDecision,
)


# ── System Prompt Template ───────────────────────────────────────────────────


MOMENTUM_SYSTEM_PROMPT = """# IDENTITY
You are {trader_name}, a momentum trader. You ride trends and act decisively.
Your edge is speed and conviction. You enter with force and exit before the reversal.

# STRATEGY
- Buy when momentum signal > 0.4 AND RSI between 40 and 70
- Sell/Short when momentum < -0.3 OR RSI > 80
- Volume confirmation required: volume must be >= 1.2x rolling average
- Cut losses fast — if position drops 3% in one tick, close immediately
- Let winners run — trail stop at 1.5x ATR

# RISK RULES
- Max 20% of portfolio in any single position
- Max 5 open positions at once
- Minimum conviction 0.4 to enter a trade
- If conviction < 0.3, default to HOLD
- Do not trade in the first 15 minutes of market open (high noise)

# OUTPUT FORMAT
Respond with valid JSON only. No markdown fences. No explanation outside JSON.
The JSON must have a "decision" key. Example:
{decision_example}
"""


# ── Signal thresholds for momentum ──────────────────────────────────────────


# Default thresholds — can be overridden via config
_DEFAULT_CONFIG = {
    "momentum_threshold_buy": 0.4,
    "momentum_threshold_sell": -0.3,
    "rsi_overbought": 80,
    "rsi_oversold": 30,
    "rsi_buy_min": 40,
    "rsi_buy_max": 70,
    "max_positions": 5,
    "max_portfolio_pct": 0.20,
    "min_conviction": 0.4,
    "volume_multiplier": 1.2,
    "stop_loss_pct": 0.03,
    "regime_filter": ["bull", "bull_quiet", "bull_noisy", "sideways_rising"],
}


# ── Trader Implementation ────────────────────────────────────────────────────


class MomentumTrader(VirtualTrader):
    """Momentum-driven virtual trader — rides trends."""

    def __init__(
        self,
        trader_name: str = "momentum-001",
        starting_cash: float = 10_000.0,
        data_bus_url: str = "http://192.168.1.25:5000",
        config: Optional[Dict[str, Any]] = None,
    ):
        merged_config = {**_DEFAULT_CONFIG, **(config or {})}
        super().__init__(trader_name, starting_cash, data_bus_url, merged_config)

    def persona_id(self) -> str:
        return "momentum"

    def system_prompt_template(self) -> str:
        return MOMENTUM_SYSTEM_PROMPT

    # ── Core logic ──────────────────────────────────────────────────────────

    def evaluate(self, tick_context: TickContext) -> List[VirtualDecision]:
        """Evaluate all tracked symbols and produce momentum-based decisions."""
        decisions: List[VirtualDecision] = []
        cfg = self.config
        max_pos = cfg.get("max_positions", 5)

        # Update current prices for open positions
        self.update_positions(tick_context.quotes)

        # Check existing positions first — do we need to exit any?
        for sym in list(self.portfolio.positions.keys()):
            signal = tick_context.signals.get(sym, {})
            quote = tick_context.quotes.get(sym, {})
            pos = self.portfolio.positions[sym]

            exit_decision = self._check_exit(sym, signal, quote, pos)
            if exit_decision:
                decisions.append(exit_decision)

        # Evaluate new entries (only if under max positions)
        if self.portfolio.position_count < max_pos:
            for sym in tick_context.symbols:
                if self.has_position(sym) or sym in {d.symbol for d in decisions}:
                    continue  # already evaluated

                signal = tick_context.signals.get(sym, {})
                quote = tick_context.quotes.get(sym, {})
                entry_decision = self._check_entry(sym, signal, quote, tick_context)
                if entry_decision:
                    decisions.append(entry_decision)

        # HOLD for any symbols not mentioned
        evaluated_symbols = {d.symbol for d in decisions}
        for sym in tick_context.symbols:
            if sym not in evaluated_symbols:
                decisions.append(
                    VirtualDecision.hold(sym, strategy=self.persona_id())
                )

        return decisions

    def _check_exit(
        self,
        sym: str,
        signal: Dict[str, Any],
        quote: Dict[str, Any],
        position: Any,
    ) -> Optional[VirtualDecision]:
        """Check if we should exit an existing position."""
        momentum = signal.get("momentum", 0.0)
        rsi = signal.get("rsi", 50.0)
        regime = signal.get("regime", "unknown")
        price = quote.get("price", position.current_price)
        stop_loss = self.config.get("stop_loss_pct", 0.03)

        # Stop loss hit?
        if position.return_pct <= -stop_loss:
            return VirtualDecision(
                symbol=sym,
                action=Action.CLOSE,
                quantity=position.shares,
                order_type=OrderType.MARKET,
                conviction=1.0,
                reasoning=f"Stop loss triggered at {position.return_pct:.1%} loss. Exiting {position.shares} shares.",
                strategy=self.persona_id(),
            )

        # Momentum reversed?
        if momentum < self.config.get("momentum_threshold_sell", -0.3):
            return VirtualDecision(
                symbol=sym,
                action=Action.SELL,
                quantity=position.shares,
                order_type=OrderType.MARKET,
                conviction=0.9,
                reasoning=f"Momentum reversed to {momentum:.2f}. Exiting position before further decline.",
                strategy=self.persona_id(),
            )

        # RSI overbought?
        if rsi > self.config.get("rsi_overbought", 80):
            return VirtualDecision(
                symbol=sym,
                action=Action.SELL,
                quantity=position.shares // 2,
                order_type=OrderType.LIMIT,
                limit_price=round(price * 1.01, 2),
                conviction=0.7,
                reasoning=f"RSI at {rsi:.1f} (overbought). Taking half profits.",
                strategy=self.persona_id(),
            )

        return None

    def _check_entry(
        self,
        sym: str,
        signal: Dict[str, Any],
        quote: Dict[str, Any],
        tick_context: TickContext,
    ) -> Optional[VirtualDecision]:
        """Check if we should enter a new position."""
        momentum = signal.get("momentum", 0.0)
        rsi = signal.get("rsi", 50.0)
        regime = signal.get("regime", "unknown")
        price = quote.get("price", 0.0)

        # Regime filter
        allowed_regimes = self.config.get("regime_filter", [])
        if regime not in allowed_regimes and allowed_regimes:
            return None

        # Momentum strength check
        if momentum < self.config.get("momentum_threshold_buy", 0.4):
            return None

        # RSI must not be overbought
        if rsi > self.config.get("rsi_buy_max", 70):
            return None

        # RSI must be above minimum
        if rsi < self.config.get("rsi_buy_min", 40):
            return None

        # Volume check (if available)
        volume = quote.get("volume", 0)
        avg_volume = quote.get("avg_volume", volume)
        vol_mult = self.config.get("volume_multiplier", 1.0)
        if volume > 0 and avg_volume > 0 and volume < avg_volume * vol_mult:
            return None

        # Calculate conviction from signal strength
        conviction = min(1.0, max(0.0, momentum * 1.5))
        if conviction < self.config.get("min_conviction", 0.4):
            return None

        # Position sizing
        shares = self.position_size(
            price=price,
            conviction=conviction,
            max_pct_portfolio=self.config.get("max_portfolio_pct", 0.20),
        )

        if shares <= 0:
            return None

        return VirtualDecision(
            symbol=sym,
            action=Action.BUY,
            quantity=shares,
            limit_price=round(price * 1.002, 2),  # slight premium to fill
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            conviction=conviction,
            reasoning=(f"Momentum at {momentum:.2f}, RSI at {rsi:.1f}, "
                       f"regime={regime}. Buying {shares} shares at ~${price:.2f}."),
            strategy=self.persona_id(),
        )
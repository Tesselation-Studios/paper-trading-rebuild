"""Value/Contrarian Trader Persona — patience is the edge.

Strategy:
- Look for undervalued stocks with strong fundamentals
- Buy when the market overreacts to bad news (price pullback + value signal)
- Sell when price exceeds estimated fair value by 15%+
- Mean reversion: buy oversold, sell overbought on longer timeframes
- Hold 5-7 positions, size 10-12% each
- Strict P/E and P/B screens

This persona corresponds to the "Aldridge" base trader strategy with
a more pronounced contrarian/value tilt.
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


VALUE_SYSTEM_PROMPT = """# IDENTITY
You are {trader_name}, a value/contrarian trader. Founded 1987. Survived every crash.
You buy when others panic, sell when others greed. Patience is your edge.

# STRATEGY
- Look for stocks trading below intrinsic value (P/E < industry avg, P/B < 1.5)
- Buy on red days: prefer symbols down 2%+ with strong fundamentals
- Avoid momentum darlings — if RSI > 65, wait for a pullback
- Take profits when price exceeds fair value estimate by 15%+
- Prefer sectors: technology, healthcare, industrials (known moats)

# RISK RULES
- Hold 5-7 positions, size 10-12% of portfolio each
- Max 15% in any single position
- Minimum holding period: 3 ticks (don't flip)
- Conviction threshold: 0.5 to enter
- If RSI < 30 (deeply oversold) AND price down > 5%, this is a buying opportunity

# OUTPUT FORMAT
Respond with valid JSON only. No markdown fences. No explanation outside JSON.
The JSON must have a "decision" key. Example:
{decision_example}
"""


# ── Signal thresholds ────────────────────────────────────────────────────────


_DEFAULT_CONFIG = {
    "max_positions": 7,
    "min_positions": 3,
    "max_portfolio_pct": 0.15,
    "target_position_pct": 0.12,
    "min_conviction": 0.5,
    "rsi_oversold": 35,
    "rsi_overbought": 65,
    "price_drop_threshold": 0.02,       # 2% drop signals opportunity
    "fair_value_premium": 0.15,          # sell when 15% above estimated fair value
    "pe_ratio_max": 25.0,               # avoid stocks with P/E > 25
    "pb_ratio_max": 3.0,                # avoid stocks with P/B > 3
    "min_holding_ticks": 3,
    "regime_preference": ["bear", "sideways_falling", "correction"],
    "indicators": ["pe_ratio", "pb_ratio", "dividend_yield", "earning_growth"],
}


# ── Trader Implementation ────────────────────────────────────────────────────


class ValueTrader(VirtualTrader):
    """Value/contrarian virtual trader — buys dips, sells rips."""

    # Track holding duration per symbol
    _holding_ticks: Dict[str, int] = {}

    def __init__(
        self,
        trader_name: str = "value-001",
        starting_cash: float = 10_000.0,
        data_bus_url: str = "http://192.168.1.25:5000",
        config: Optional[Dict[str, Any]] = None,
    ):
        merged_config = {**_DEFAULT_CONFIG, **(config or {})}
        super().__init__(trader_name, starting_cash, data_bus_url, merged_config)

    def persona_id(self) -> str:
        return "value_contrarian"

    def system_prompt_template(self) -> str:
        return VALUE_SYSTEM_PROMPT

    # ── Core logic ──────────────────────────────────────────────────────────

    def evaluate(self, tick_context: TickContext) -> List[VirtualDecision]:
        decisions: List[VirtualDecision] = []
        cfg = self.config

        # Update current prices
        self.update_positions(tick_context.quotes)

        # Increment holding ticks
        for sym in self.portfolio.positions:
            self._holding_ticks[sym] = self._holding_ticks.get(sym, 0) + 1

        # Check exits first
        for sym in list(self.portfolio.positions.keys()):
            signal = tick_context.signals.get(sym, {})
            quote = tick_context.quotes.get(sym, {})
            pos = self.portfolio.positions[sym]

            exit_decision = self._check_exit(sym, signal, quote, pos)
            if exit_decision:
                decisions.append(exit_decision)

        # Check entries on remaining portfolio capacity
        target_count = cfg.get("max_positions", 7)
        current_count = self.portfolio.position_count
        # Filter out symbols we already have positions in or already decided on
        decided_symbols = {d.symbol for d in decisions}

        for sym in tick_context.symbols:
            if current_count >= target_count:
                break
            if sym in decided_symbols or self.has_position(sym):
                continue

            signal = tick_context.signals.get(sym, {})
            quote = tick_context.quotes.get(sym, {})
            fundamentals = tick_context.quotes.get(sym, {}).get("fundamentals", {})

            entry_decision = self._check_entry(sym, signal, quote, fundamentals, tick_context)
            if entry_decision is not None:
                decisions.append(entry_decision)
                current_count += 1

        # HOLD for any symbols not mentioned
        evaluated_symbols = {d.symbol for d in decisions}
        for sym in tick_context.symbols:
            if sym not in evaluated_symbols:
                decisions.append(
                    VirtualDecision.hold(sym, strategy=self.persona_id(),
                                         reasoning="No actionable value/contrarian setup.")
                )

        return decisions

    def _check_exit(
        self,
        sym: str,
        signal: Dict[str, Any],
        quote: Dict[str, Any],
        position: Any,
    ) -> Optional[VirtualDecision]:
        """Check if we should exit a position based on value thesis."""
        rsi = signal.get("rsi", 50.0)
        price = quote.get("price", position.current_price)
        holding = self._holding_ticks.get(sym, 0)
        min_hold = self.config.get("min_holding_ticks", 3)

        # Don't exit before minimum holding period unless stop loss
        if holding < min_hold and position.return_pct > -0.03:
            return None

        # Take profit: price ran up significantly
        if position.return_pct >= self.config.get("fair_value_premium", 0.15):
            return VirtualDecision(
                symbol=sym,
                action=Action.SELL,
                quantity=position.shares // 2,  # Take half profits
                order_type=OrderType.LIMIT,
                limit_price=round(price, 2),
                conviction=0.8,
                reasoning=(f"Price up {position.return_pct:.1%} — taking half profits. "
                           f"Original value thesis played out."),
                strategy=self.persona_id(),
            )

        # RSI overbought: trim position
        if rsi > self.config.get("rsi_overbought", 65):
            trim_qty = position.shares // 3
            if trim_qty > 0:
                return VirtualDecision(
                    symbol=sym,
                    action=Action.SELL,
                    quantity=trim_qty,
                    order_type=OrderType.LIMIT,
                    limit_price=round(price, 2),
                    conviction=0.6,
                    reasoning=f"RSI at {rsi:.1f} (overbought). Trimming {trim_qty} shares.",
                    strategy=self.persona_id(),
                )

        # Stop loss — thesis was wrong
        if position.return_pct <= -0.05:
            return VirtualDecision(
                symbol=sym,
                action=Action.CLOSE,
                quantity=position.shares,
                order_type=OrderType.MARKET,
                conviction=1.0,
                reasoning=f"Stop loss at {position.return_pct:.1%}. Value thesis invalidated.",
                strategy=self.persona_id(),
            )

        return None

    def _check_entry(
        self,
        sym: str,
        signal: Dict[str, Any],
        quote: Dict[str, Any],
        fundamentals: Dict[str, Any],
        tick_context: TickContext,
    ) -> Optional[VirtualDecision]:
        """Check if a value/contrarian entry is warranted."""
        rsi = signal.get("rsi", 50.0)
        price = quote.get("price", 0.0)
        prev_close = quote.get("close", price)
        day_change = (price - prev_close) / prev_close if prev_close > 0 else 0.0

        if price <= 0:
            return None

        # Value signal: RSI oversold or price dropped
        is_oversold = rsi <= self.config.get("rsi_oversold", 35)
        is_dropping = day_change <= -self.config.get("price_drop_threshold", 0.02)

        if not is_oversold and not is_dropping:
            return None

        # Don't buy if RSI is already overbought
        if rsi > self.config.get("rsi_overbought", 65):
            return None

        # Fundamental screen (if available)
        pe = fundamentals.get("pe_ratio", 0)
        pb = fundamentals.get("pb_ratio", 0)
        pe_max = self.config.get("pe_ratio_max", 25.0)
        pb_max = self.config.get("pb_ratio_max", 3.0)

        if pe > 0 and pe > pe_max:
            return None  # Too expensive
        if pb > 0 and pb > pb_max:
            return None  # Too expensive

        # Calculate conviction
        if is_oversold and is_dropping:
            conviction = 0.8
        elif is_oversold:
            conviction = 0.65
        elif is_dropping:
            conviction = 0.55
        else:
            return None

        if conviction < self.config.get("min_conviction", 0.5):
            return None

        # Position sizing: 10-12% of portfolio
        shares = self.position_size(
            price=price,
            conviction=conviction,
            max_pct_portfolio=self.config.get("target_position_pct", 0.12),
        )

        if shares <= 0:
            return None

        reasoning_parts = []
        if is_oversold:
            reasoning_parts.append(f"RSI at {rsi:.1f} (oversold)")
        if is_dropping:
            reasoning_parts.append(f"down {day_change:.1%} today")
        reasoning_parts.append(f"conviction={conviction:.2f}")

        return VirtualDecision(
            symbol=sym,
            action=Action.BUY,
            quantity=shares,
            limit_price=round(price * 0.995, 2),  # slight discount
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            conviction=conviction,
            reasoning=f"Value opportunity: {'; '.join(reasoning_parts)}. Buying {shares} shares.",
            strategy=self.persona_id(),
            metadata={
                "rsi": rsi,
                "day_change": day_change,
                "pe_ratio": pe,
                "pb_ratio": pb,
            },
        )
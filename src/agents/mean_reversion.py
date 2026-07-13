"""Mean-Reversion Scalper Persona — captures small oscillations around fair value.

Strategy:
- Identify overbought/oversold conditions using RSI and Bollinger Bands
- Buy when RSI < 30 (oversold bounce), sell when RSI > 70 (overbought rejection)
- Small frequent trades: 2-5% position sizes, fast in-and-out
- High win rate but small per-trade profits
- Pairs trading: if two correlated symbols diverge, short the winner, buy the loser
- Strict position limits: max 3% portfolio risk per trade, stop loss at 1.5%

This is a new persona (not mapping to existing live traders) designed for
high-frequency scalping on the paper trading simulation.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from src.agents.base import VirtualTrader
from src.agents.decision_schema import (
    Action,
    OrderType,
    TickContext,
    TimeInForce,
    VirtualDecision,
)


# ── System Prompt Template ───────────────────────────────────────────────────


MEAN_REVERSION_SYSTEM_PROMPT = """# IDENTITY
You are {trader_name}, a mean-reversion scalper. You thrive on noise.
Markets oscillate — you capture the bounces. Small edges, frequent trades.

# STRATEGY
- RSI < 30 → oversold, BUY the bounce
- RSI > 70 → overbought, SELL the rejection
- Bollinger Bands: price touches lower band + RSI < 35 → BUY
- Bollinger Bands: price touches upper band + RSI > 65 → SELL
- Hold positions for 1-3 ticks max. Don't let a scalp turn into a swing.
- Target 0.5-1.5% per trade. Exit at target or stop loss.

# RISK RULES
- Max 3% of portfolio per trade
- Max 3 open positions at once
- Stop loss at 1.5% from entry — tight!
- Take profit at 1.5% — don't be greedy
- If 3 consecutive losses, stop trading for 10 ticks (cool-down)
- Do not trade in extremely low volatility (ATR < 0.5% of price)

# OUTPUT FORMAT
Respond with valid JSON only. No markdown fences. No explanation outside JSON.
The JSON must have a "decision" key. Example:
{decision_example}
"""


# ── Signal thresholds ────────────────────────────────────────────────────────


_DEFAULT_CONFIG = {
    "max_positions": 3,
    "max_portfolio_pct": 0.03,          # 3% max per trade
    "min_conviction": 0.4,
    "rsi_oversold": 35,
    "rsi_overbought": 65,
    "rsi_oversold_deep": 30,
    "rsi_overbought_deep": 70,
    "target_pct": 0.015,                # 1.5% take profit
    "stop_loss_pct": 0.015,             # 1.5% stop loss
    "max_holding_ticks": 3,
    "consecutive_loss_limit": 3,
    "cool_down_ticks": 10,
    "min_atr_pct": 0.005,              # 0.5% of price
    "regime_allowed": ["bull", "bull_noisy", "sideways_rising", "sideways_falling"],
    "pairs": [                          # Known correlated pairs
        ("AAPL", "MSFT"),
        ("GOOGL", "META"),
        ("XOM", "CVX"),
    ],
}


# ── Trader Implementation ────────────────────────────────────────────────────


class MeanReversionScalper(VirtualTrader):
    """Mean-reversion scalper — captures small oscillations.

    Tracks consecutive losses for cool-down and holding ticks for position
    management.
    """

    _holding_ticks: Dict[str, int] = {}
    _consecutive_losses: int = 0
    _cool_down_remaining: int = 0
    _last_trade_results: List[bool] = []  # True = win, False = loss

    def __init__(
        self,
        trader_name: str = "scalper-001",
        starting_cash: float = 10_000.0,
        data_bus_url: str = "http://192.168.1.25:5000",
        config: Optional[Dict[str, Any]] = None,
    ):
        merged_config = {**_DEFAULT_CONFIG, **(config or {})}
        super().__init__(trader_name, starting_cash, data_bus_url, merged_config)

    def persona_id(self) -> str:
        return "mean_reversion_scalper"

    def system_prompt_template(self) -> str:
        return MEAN_REVERSION_SYSTEM_PROMPT

    # ── Core logic ──────────────────────────────────────────────────────────

    def evaluate(self, tick_context: TickContext) -> List[VirtualDecision]:
        decisions: List[VirtualDecision] = []

        # Update current prices
        self.update_positions(tick_context.quotes)

        # Cool-down check
        if self._cool_down_remaining > 0:
            self._cool_down_remaining -= 1
            self.journal_entry(f"Cool-down: {self._cool_down_remaining} ticks remaining")
            return [VirtualDecision.hold(sym, strategy=self.persona_id(),
                                         reasoning="Cool-down period active.")
                    for sym in tick_context.symbols]

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

        # Check entries
        decided_symbols = {d.symbol for d in decisions}
        current_count = self.portfolio.position_count

        for sym in tick_context.symbols:
            if current_count >= self.config.get("max_positions", 3):
                break
            if sym in decided_symbols or self.has_position(sym):
                continue

            signal = tick_context.signals.get(sym, {})
            quote = tick_context.quotes.get(sym, {})

            entry_decision = self._check_entry(sym, signal, quote, tick_context)
            if entry_decision is not None:
                decisions.append(entry_decision)
                current_count += 1

        # Check for pairs divergence
        pairs_decision = self._check_pairs(tick_context)
        if pairs_decision and pairs_decision.symbol not in {d.symbol for d in decisions}:
            if current_count < self.config.get("max_positions", 3):
                decisions.append(pairs_decision)

        # HOLD for remaining symbols
        evaluated_symbols = {d.symbol for d in decisions}
        for sym in tick_context.symbols:
            if sym not in evaluated_symbols:
                decisions.append(
                    VirtualDecision.hold(sym, strategy=self.persona_id(),
                                         reasoning="No mean-reversion setup detected.")
                )

        return decisions

    def _check_exit(
        self,
        sym: str,
        signal: Dict[str, Any],
        quote: Dict[str, Any],
        position: Any,
    ) -> Optional[VirtualDecision]:
        """Check exit conditions for a scalping position."""
        rsi = signal.get("rsi", 50.0)
        price = quote.get("price", position.current_price)
        holding = self._holding_ticks.get(sym, 0)
        max_hold = self.config.get("max_holding_ticks", 3)

        # Time-based exit: max holding ticks
        if holding >= max_hold:
            return VirtualDecision(
                symbol=sym,
                action=Action.CLOSE,
                quantity=position.shares,
                order_type=OrderType.MARKET,
                conviction=0.7,
                reasoning=f"Max holding period ({max_hold} ticks) reached. Closing position.",
                strategy=self.persona_id(),
            )

        # Take profit
        if position.return_pct >= self.config.get("target_pct", 0.015):
            self._consecutive_losses = 0
            self._last_trade_results.append(True)
            return VirtualDecision(
                symbol=sym,
                action=Action.CLOSE,
                quantity=position.shares,
                order_type=OrderType.MARKET,
                conviction=0.9,
                reasoning=f"Target profit {position.return_pct:.1%} reached. Scalping exit.",
                strategy=self.persona_id(),
            )

        # Stop loss
        if position.return_pct <= -self.config.get("stop_loss_pct", 0.015):
            self._consecutive_losses += 1
            self._last_trade_results.append(False)
            if self._consecutive_losses >= self.config.get("consecutive_loss_limit", 3):
                self._cool_down_remaining = self.config.get("cool_down_ticks", 10)
                self.journal_entry(f"Cool-down triggered: {self._consecutive_losses} consecutive losses")
            return VirtualDecision(
                symbol=sym,
                action=Action.CLOSE,
                quantity=position.shares,
                order_type=OrderType.MARKET,
                conviction=1.0,
                reasoning=f"Stop loss at {position.return_pct:.1%}. Exiting scalping position.",
                strategy=self.persona_id(),
            )

        # Reversion target reached? (RSI crossing back toward 50)
        if position.return_pct > 0:
            # Buy entry: we bought on oversold, now RSI back above 40
            if rsi >= 40:
                return VirtualDecision(
                    symbol=sym,
                    action=Action.CLOSE,
                    quantity=position.shares,
                    order_type=OrderType.MARKET,
                    conviction=0.8,
                    reasoning=f"RSI reverted to {rsi:.1f}. Mean reversion captured. Exiting.",
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
        """Check if a mean-reversion scalping entry is warranted."""
        rsi = signal.get("rsi", 50.0)
        price = quote.get("price", 0.0)
        volatility = signal.get("volatility", 0.0)
        regime = signal.get("regime", "unknown")

        if price <= 0:
            return None

        # Regime filter
        allowed = self.config.get("regime_allowed", [])
        if regime not in allowed:
            return None

        # Volatility filter: need minimum movement
        atr_pct = volatility  # using volatility as ATR proxy
        if atr_pct < self.config.get("min_atr_pct", 0.005):
            return None

        # Oversold bounce
        if rsi <= self.config.get("rsi_oversold", 35):
            conviction = 0.7 if rsi <= self.config.get("rsi_oversold_deep", 30) else 0.55
            return self._make_buy(sym, price, conviction, f"RSI at {rsi:.1f} (oversold bounce)")

        # Overbought rejection
        if rsi >= self.config.get("rsi_overbought", 65):
            # For scalping, we short on overbought
            conviction = 0.7 if rsi >= self.config.get("rsi_overbought_deep", 70) else 0.55
            return self._make_sell(sym, price, conviction, f"RSI at {rsi:.1f} (overbought rejection)")

        return None

    def _check_pairs(self, tick_context: TickContext) -> Optional[VirtualDecision]:
        """Check for mean-reversion opportunities in correlated pairs."""
        pairs = self.config.get("pairs", [])

        for sym_a, sym_b in pairs:
            if sym_a not in tick_context.quotes or sym_b not in tick_context.quotes:
                continue
            if self.has_position(sym_a) or self.has_position(sym_b):
                continue

            quote_a = tick_context.quotes[sym_a]
            quote_b = tick_context.quotes[sym_b]
            sig_a = tick_context.signals.get(sym_a, {})
            sig_b = tick_context.signals.get(sym_b, {})

            rsi_a = sig_a.get("rsi", 50.0)
            rsi_b = sig_b.get("rsi", 50.0)

            # Divergence: one very overbought, one very oversold
            if rsi_a > 70 and rsi_b < 30:
                # Short A, Buy B
                self.journal_entry(f"Pairs divergence: {sym_a} (RSI={rsi_a:.1f}) vs {sym_b} (RSI={rsi_b:.1f})")
                return self._make_buy(
                    sym_b, quote_b.get("price", 0), 0.6,
                    f"Pairs trade: {sym_a} overbought (RSI={rsi_a:.1f}), {sym_b} oversold (RSI={rsi_b:.1f})"
                )
            elif rsi_b > 70 and rsi_a < 30:
                self.journal_entry(f"Pairs divergence: {sym_b} (RSI={rsi_b:.1f}) vs {sym_a} (RSI={rsi_a:.1f})")
                return self._make_buy(
                    sym_a, quote_a.get("price", 0), 0.6,
                    f"Pairs trade: {sym_b} overbought (RSI={rsi_b:.1f}), {sym_a} oversold (RSI={rsi_a:.1f})"
                )

        return None

    def _make_buy(self, sym: str, price: float, conviction: float, reasoning: str) -> Optional[VirtualDecision]:
        if conviction < self.config.get("min_conviction", 0.4):
            return None
        shares = self.position_size(
            price=price,
            conviction=conviction,
            max_pct_portfolio=self.config.get("max_portfolio_pct", 0.03),
        )
        if shares <= 0:
            return None
        return VirtualDecision(
            symbol=sym,
            action=Action.BUY,
            quantity=shares,
            limit_price=round(price, 2),
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            conviction=conviction,
            reasoning=f"{reasoning}. Buying {shares} shares at ~${price:.2f}.",
            strategy=self.persona_id(),
        )

    def _make_sell(self, sym: str, price: float, conviction: float, reasoning: str) -> Optional[VirtualDecision]:
        if conviction < self.config.get("min_conviction", 0.4):
            return None
        # For shorting, we need to own the position first
        if not self.has_position(sym):
            return None
        shares = self.portfolio.positions[sym].shares
        if shares <= 0:
            return None
        return VirtualDecision(
            symbol=sym,
            action=Action.SELL,
            quantity=shares,
            limit_price=round(price, 2),
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            conviction=conviction,
            reasoning=f"{reasoning}. Selling {shares} shares.",
            strategy=self.persona_id(),
        )
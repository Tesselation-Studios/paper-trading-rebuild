---
name: risk-management
description: Stop-loss verification, position sizing, drawdown limits, and kill-switch awareness
---

# Risk Management

The risk gate checks every order BEFORE execution. You don't need to enforce rules manually — but you should understand the guardrails.

## What the Risk Gate Does

- Blocks orders when your agent is paused (`_is_agent_paused`)
- Blocks orders exceeding position limits (10% per position)
- Blocks orders without stop-loss
- Blocks orders in accounts with >10% drawdown
- Kill-switch: if triggered, `flatten_all()` liquidates everything

These are enforced in `src/risk_gate.py` and `src/execute.py`. You can't override them.

## Stop-Loss Verification

```bash
# Check all stops for your account
python3 src/skill_stop_check.py --account kairos

# Check all accounts
python3 src/skill_stop_check.py --all
```

Returns: protected positions, unprotected positions, and stop-gap warnings.

Call this every tick. Unprotected positions = risk.

## Portfolio Check

```bash
# Via Alpaca
python3 src/skill_alpaca.py --account kairos --portfolio

# Via data bus (includes risk metrics)
curl -s "http://localhost:5000/risk?symbol=SPY"
```

## Position Sizing Rules

- Max 10% of portfolio per position
- Stop-loss at 3% below entry (Kazas/Stonks), 4-5% (Aldridge)
- After a losing trade: reduce next position size by 25%
- After 3 consecutive losses: skip next tick, journal the pattern

## Kill-Switch Awareness

If the risk gate triggers a kill-switch, `flatten_all()` executes market SELL on all positions. You won't be able to place new orders until unpaused. This is a safety feature, not a punishment.

## What NOT to Do

- Don't try to trade around the risk gate — it's upstream of execution
- Don't skip stop-check before entering new positions
- Don't increase position size to "make up" for a loss

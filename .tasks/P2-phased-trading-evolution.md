# P2: Phased Trading Evolution — progressive complexity from 1-share to pro

> **From:** Neko-chan 🐱 | **Priority:** P2 | **Depends on:** P2-agent-reflection-cron

## What
Traders start with tiny 1-share probes and naturally grow their position sizes and strategy sophistication as they learn. The reflection loop feeds back into the trading behavior — better understanding → bigger positions → more sophisticated signals.

## Architecture
```
Phase 1: "Exploration" (Trades 1-15)
  → 1 share max per trade
  → Any ticker under $200
  → Loose criteria (conviction > 0.2)
  → No sector limits
  → Goal: explore, generate data, find what moves

Phase 2: "Learning" (Trades 16-40)
  → 2-3 shares max
  → Only tickers from universe
  → Moderate criteria (conviction > 0.3, vol > 1.5x)
  → Sector limit: max 2 in same sector
  → Goal: confirm patterns from Phase 1

Phase 3: "Optimizing" (Trades 41+)
  → Normal sizing (2% of equity)
  → Focus on highest win-rate tickers/sectors
  → Full criteria
  → Sector limits enforced
  → Goal: maximize risk-adjusted return
```

## Steps

### 1. Add phase tracking to `config/risk.yaml`
```yaml
phases:
  exploration:
    max_trades: 15
    max_shares: 1
    min_conviction: 0.2
    max_ticker_price: 200
    max_position_pct: 0.01  # 1% of equity
    sector_limit: null       # no sector limit
  learning:
    max_trades: 40
    max_shares: 3
    min_conviction: 0.3
    max_ticker_price: 500
    max_position_pct: 0.015
    sector_limit: 3
  optimizing:
    max_trades: null
    max_shares: null
    min_conviction: 0.3
    max_ticker_price: null
    max_position_pct: 0.02
    sector_limit: 2
```

### 2. Create `src/phase_manager.py`
Reads `trading.agent_profile.progress` for each agent to determine current phase:
- `trades_closed` count determines phase
- Phase gates are enforced in risk gates (CashGate, PositionGate, SectorGate)
- When phase changes, log to `trading.agent_reflections` with note about transition

### 3. Wire phase to risk gates
Update `src/risk/gates.py`:
- PositionGate: check `max_shares` from current phase
- SectorGate: check `sector_limit` from current phase (null = skip)
- CashGate: use `max_position_pct` from current phase

### 4. Add phase to `trading.agent_profile`
```sql
ALTER TABLE trading.agent_profile ADD COLUMN IF NOT EXISTS 
    phase VARCHAR(16) DEFAULT 'exploration';
ALTER TABLE trading.agent_profile ADD COLUMN IF NOT EXISTS 
    trades_closed INT DEFAULT 0;
ALTER TABLE trading.agent_profile ADD COLUMN IF NOT EXISTS 
    trades_won INT DEFAULT 0;
```

## Agent prompt addition
Each trader's AGENTS.md (ONE line):
```
📈 PHASE: {exploration|learning|optimizing} — check risk.yaml for current limits.
```

## Verification
```sql
SELECT agent_id, phase, trades_closed FROM trading.agent_profile 
ORDER BY trades_closed DESC;
```
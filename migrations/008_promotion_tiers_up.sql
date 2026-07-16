-- Migration 008: Virtual trader promotion tiers
-- Adds tier tracking to virtual_traders, extends promotion_log
-- for multi-tier promotions and demotions.
-- Spec: specs/virtual-trader-promotion.md

-- ══════════════════════════════════════════════════════════════════════
-- 1. Add tier columns to virtual_traders
-- ══════════════════════════════════════════════════════════════════════

ALTER TABLE trading.virtual_traders
  ADD COLUMN IF NOT EXISTS tier VARCHAR(16) DEFAULT 'probation',
  ADD COLUMN IF NOT EXISTS tier_promoted_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS tier_demoted_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS tier_cooldown_until DATE,
  ADD COLUMN IF NOT EXISTS tier_warning_count INTEGER DEFAULT 0,
  ADD COLUMN IF NOT EXISTS total_demotions INTEGER DEFAULT 0,
  ADD COLUMN IF NOT EXISTS error_count_24h INTEGER DEFAULT 0,
  ADD COLUMN IF NOT EXISTS last_decision_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS closed_trades INTEGER DEFAULT 0,
  ADD COLUMN IF NOT EXISTS age_trading_days INTEGER DEFAULT 0,
  ADD COLUMN IF NOT EXISTS llm_errors INTEGER DEFAULT 0;

-- Upgrade existing records with appropriate tiers
UPDATE trading.virtual_traders
  SET tier = CASE
    WHEN status = 'live' THEN 'live'
    WHEN status = 'probation' THEN 'probation'
    WHEN status = 'active' AND is_champion = TRUE THEN 'elite'
    WHEN status = 'active' THEN 'veteran'
    WHEN status = 'culled' THEN 'probation'
    ELSE 'probation'
  END
  WHERE tier IS NULL OR tier = 'probation';

-- ══════════════════════════════════════════════════════════════════════
-- 2. Indexes for promotion query performance
-- ══════════════════════════════════════════════════════════════════════

CREATE INDEX IF NOT EXISTS idx_vt_tier ON trading.virtual_traders (tier);
CREATE INDEX IF NOT EXISTS idx_vt_base_tier ON trading.virtual_traders (base_trader, tier);
CREATE INDEX IF NOT EXISTS idx_vt_status_tier ON trading.virtual_traders (status, tier);

-- ══════════════════════════════════════════════════════════════════════
-- 3. Extend promotion_log for multi-tier tracking
-- ══════════════════════════════════════════════════════════════════════

ALTER TABLE trading.promotion_log
  ADD COLUMN IF NOT EXISTS tier_from VARCHAR(16),
  ADD COLUMN IF NOT EXISTS tier_to VARCHAR(16),
  ADD COLUMN IF NOT EXISTS promotion_type VARCHAR(16) DEFAULT 'promotion',
  ADD COLUMN IF NOT EXISTS warnings_sent INTEGER DEFAULT 0,
  ADD COLUMN IF NOT EXISTS grace_expires_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS suppression_reason TEXT;

-- Upgrade existing rows
UPDATE trading.promotion_log
  SET tier_from = 'elite', tier_to = 'live', promotion_type = 'promotion'
  WHERE tier_from IS NULL AND was_rolled_back = FALSE;

UPDATE trading.promotion_log
  SET tier_from = 'live', tier_to = 'elite', promotion_type = 'rollback'
  WHERE tier_from IS NULL AND was_rolled_back = TRUE;

-- ══════════════════════════════════════════════════════════════════════
-- 4. Daily tier snapshots (for dashboards)
-- ══════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS trading.tier_snapshots (
  id              BIGSERIAL PRIMARY KEY,
  date            DATE            NOT NULL,
  base_trader     VARCHAR(32)     NOT NULL,
  tier            VARCHAR(32)     NOT NULL,
  count           INTEGER         NOT NULL DEFAULT 0,
  filled_slots    INTEGER         NOT NULL DEFAULT 0,
  total_slots     INTEGER         NOT NULL DEFAULT 0,
  avg_composite   DECIMAL,
  avg_return_pct  DECIMAL,
  created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_tier_snapshot UNIQUE (date, base_trader, tier)
);

CREATE INDEX IF NOT EXISTS idx_tier_snapshots_date ON trading.tier_snapshots (date DESC);

-- ══════════════════════════════════════════════════════════════════════
-- 5. Per-event promotion summary (one row per promotion event)
-- ══════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS trading.promotion_summary (
  id              BIGSERIAL PRIMARY KEY,
  trader_id       VARCHAR(32)     NOT NULL,
  virtual_name    VARCHAR(64)     NOT NULL,
  from_tier       VARCHAR(32)     NOT NULL,
  to_tier         VARCHAR(32)     NOT NULL,
  composite_score DECIMAL,
  calmar          DECIMAL,
  sortino         DECIMAL,
  profit_factor   DECIMAL,
  win_rate        DECIMAL,
  total_return_pct DECIMAL,
  max_drawdown    DECIMAL,
  n_trades        INTEGER,
  reason          TEXT,
  promoted_by     VARCHAR(64)     DEFAULT 'system',
  created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_promotion_summary_trader
  ON trading.promotion_summary (trader_id, created_at DESC);

-- ══════════════════════════════════════════════════════════════════════
-- 6. Promotion config as system param (JSON)
-- ══════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS trading.system_params (
  key VARCHAR(128) PRIMARY KEY,
  value JSONB NOT NULL,
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO trading.system_params (key, value) VALUES ('promotion_config', '{
  "probation_to_rookie": {
    "min_days": 2,
    "min_trades": 1,
    "max_llm_errors": 0
  },
  "rookie_to_veteran": {
    "min_days": 5,
    "min_trades": 5,
    "pnl_window_days": 7,
    "pnl_threshold": 0.0
  },
  "veteran_to_expert": {
    "min_days": 14,
    "min_trades": 10,
    "win_rate_window": 20,
    "win_rate_threshold": 0.50,
    "pnl_window_days": 14,
    "pnl_baseline_multiplier": 1.05
  },
  "expert_to_elite": {
    "min_days": 30,
    "min_trades": 20,
    "sharpe_window_days": 30,
    "sharpe_threshold": 0.8,
    "alpha_window_days": 30,
    "alpha_multiplier": 1.10
  },
  "elite_to_live": {
    "min_closed_trades": 10,
    "main_min_days": 3,
    "lock_streak_days": 3,
    "max_lock_days": 5,
    "all_negative_freeze_days": 3
  },
  "demotion": {
    "max_errors_24h": 5,
    "pnl_drawdown_threshold": -0.10,
    "consecutive_negative_days": 10,
    "win_rate_minimum": 0.30,
    "win_rate_window": 20,
    "sharpe_minimum": 0.0,
    "sharpe_window_days": 30,
    "inactivity_days": 5,
    "demotion_cooldown_days": 5,
    "max_demotions_30d_before_cull": 3,
    "grace_warning_days": 3
  }
}'::jsonb)
ON CONFLICT (key) DO NOTHING;

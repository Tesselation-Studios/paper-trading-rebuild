-- Migration 008 rollback: Remove promotion tier system
-- Spec: specs/virtual-trader-promotion.md

-- Remove tables added by 008_promotion_tiers_up.sql
DROP TABLE IF EXISTS trading.tier_snapshots CASCADE;
DROP TABLE IF EXISTS trading.promotion_summary CASCADE;
DROP TABLE IF EXISTS trading.system_params CASCADE;

-- Remove extended columns from promotion_log
ALTER TABLE trading.promotion_log
  DROP COLUMN IF EXISTS tier_from,
  DROP COLUMN IF EXISTS tier_to,
  DROP COLUMN IF EXISTS promotion_type,
  DROP COLUMN IF EXISTS warnings_sent,
  DROP COLUMN IF EXISTS grace_expires_at,
  DROP COLUMN IF EXISTS suppression_reason;

-- Remove tier indexes
DROP INDEX IF EXISTS idx_vt_tier;
DROP INDEX IF EXISTS idx_vt_base_tier;
DROP INDEX IF EXISTS idx_vt_status_tier;

-- Remove tier columns from virtual_traders
ALTER TABLE trading.virtual_traders
  DROP COLUMN IF EXISTS tier,
  DROP COLUMN IF EXISTS tier_promoted_at,
  DROP COLUMN IF EXISTS tier_demoted_at,
  DROP COLUMN IF EXISTS tier_cooldown_until,
  DROP COLUMN IF EXISTS tier_warning_count,
  DROP COLUMN IF EXISTS total_demotions,
  DROP COLUMN IF EXISTS error_count_24h,
  DROP COLUMN IF EXISTS last_decision_at,
  DROP COLUMN IF EXISTS closed_trades,
  DROP COLUMN IF EXISTS age_trading_days,
  DROP COLUMN IF EXISTS llm_errors;

-- Remove system params (only promo-related)
DELETE FROM trading.system_params WHERE key = 'promotion_config';
DELETE FROM trading.system_params WHERE key = 'promotions_frozen';

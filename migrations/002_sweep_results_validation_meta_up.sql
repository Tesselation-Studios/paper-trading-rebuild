-- Migration: Add validation_meta JSONB column to sweep_results
-- Supports two-phase validation (signal + LLM) logging
-- Up: 002_sweep_results_validation_meta_up.sql

ALTER TABLE trading.sweep_results
    ADD COLUMN IF NOT EXISTS validation_meta JSONB;

COMMENT ON COLUMN trading.sweep_results.validation_meta IS
    'Two-phase validation metadata: variant_name, phase1_winner, phase2_winner, signal_llm_divergence, etc.';
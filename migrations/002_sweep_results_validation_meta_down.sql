-- Migration: Remove validation_meta JSONB column from sweep_results
-- Down: 002_sweep_results_validation_meta_down.sql

ALTER TABLE trading.sweep_results
    DROP COLUMN IF EXISTS validation_meta;
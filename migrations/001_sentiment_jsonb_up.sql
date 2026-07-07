-- Migration 001: Convert sentiment.source from TEXT to JSONB
-- Applied: 2026-07-06
-- Up: Casts source column to JSONB + adds GIN index
-- Down: Casts back to TEXT, drops GIN index

-- Verify all rows are valid JSON before conversion (ran manually: 68/68 valid)

ALTER TABLE trading.sentiment ALTER COLUMN source TYPE jsonb USING source::jsonb;

CREATE INDEX IF NOT EXISTS idx_sentiment_source ON trading.sentiment USING GIN (source);

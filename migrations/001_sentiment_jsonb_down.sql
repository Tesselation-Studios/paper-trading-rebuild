-- Migration 001: Revert sentiment.source from JSONB back to TEXT
-- Down: Drops GIN index, casts back to TEXT

DROP INDEX IF EXISTS idx_sentiment_source;

ALTER TABLE trading.sentiment ALTER COLUMN source TYPE text;

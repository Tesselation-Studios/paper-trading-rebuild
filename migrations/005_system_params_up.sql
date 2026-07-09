-- ============================================================================
-- Migration 005: system_params table
-- Task: Seed system_params with hardcoded defaults from source code.
-- Card: bca970e2-cbf3-49a3-a54c-176a0a3addea
--
-- system_params stores system-wide operational defaults that were previously
-- hardcoded as Python function defaults. Per-trader overrides live in
-- trading.params and agents/*/config.yaml.
-- ============================================================================

CREATE TABLE IF NOT EXISTS trading.system_params (
    id           BIGSERIAL PRIMARY KEY,
    param_name   VARCHAR(128)   NOT NULL,
    param_value  NUMERIC        NOT NULL,
    param_type   VARCHAR(16)    NOT NULL DEFAULT 'float',  -- float, int, bool
    description  TEXT,
    source_file  VARCHAR(128),   -- which source file the default came from
    created_at   TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_system_params_name UNIQUE (param_name)
);

-- ── Signal Engine defaults (src/signals.py: SignalParams) ────────────────────
INSERT INTO trading.system_params (param_name, param_value, param_type, description, source_file)
VALUES
    ('signal.momentum_threshold',       0.55,  'float', 'Momentum composite signal threshold for BUY',    'src/signals.py'),
    ('signal.momentum_lookback',        20,    'int',   'Lookback window for momentum calculation',        'src/signals.py'),
    ('signal.momentum_decay',           0.85,  'float', 'EMA decay factor for momentum smoothing',         'src/signals.py'),
    ('signal.rsi_oversold',             30.0,  'float', 'RSI oversold threshold',                          'src/signals.py'),
    ('signal.rsi_overbought',           70.0,  'float', 'RSI overbought threshold',                        'src/signals.py'),
    ('signal.bollinger_std',            2.0,   'float', 'Bollinger Band standard deviation multiplier',    'src/signals.py'),
    ('signal.volume_threshold',         1.2,   'float', 'Min volume / 20d avg volume ratio',               'src/signals.py'),
    ('signal.vol_regime_threshold',     0.25,  'float', 'Volatility regime classification threshold',      'src/signals.py'),
    ('signal.vol_reduction_multiplier', 0.7,   'float', 'Position size multiplier in high-volatility',    'src/signals.py'),
    ('signal.base_size_pct',            0.15,  'float', 'Base position size as % of equity',               'src/signals.py'),
    ('signal.conviction_multiplier',    1.5,   'float', 'Conviction scaling multiplier for position size', 'src/signals.py'),
    ('signal.max_positions',            5,     'int',   'Max concurrent positions',                        'src/signals.py'),
    ('signal.stop_loss_pct',            0.05,  'float', 'Default stop-loss % from entry',                  'src/signals.py'),
    ('signal.take_profit_pct',          0.15,  'float', 'Default take-profit % from entry',                'src/signals.py'),
    ('signal.trailing_stop_pct',        0.03,  'float', 'Default trailing stop %',                         'src/signals.py'),
    ('signal.weight_trending_up',       1.0,   'float', 'Regime weight: trending up',                      'src/signals.py'),
    ('signal.weight_trending_down',     0.5,   'float', 'Regime weight: trending down',                    'src/signals.py'),
    ('signal.weight_mean_reverting',    0.8,   'float', 'Regime weight: mean-reverting',                   'src/signals.py'),
    ('signal.weight_high_volatility',   0.4,   'float', 'Regime weight: high volatility',                  'src/signals.py')
ON CONFLICT (param_name) DO NOTHING;

-- ── Speculative Risk defaults (src/risk/manager.py: _build_default_gates) ───
INSERT INTO trading.system_params (param_name, param_value, param_type, description, source_file)
VALUES
    ('spec_risk.max_position_pct',  0.20, 'float', 'Max single-position % of portfolio (gate default)', 'src/risk/manager.py'),
    ('spec_risk.max_exposure_pct',  1.00, 'float', 'Max total exposure % of portfolio (gate default)',  'src/risk/manager.py'),
    ('spec_risk.pdt_day_trade_limit', 3,  'int',   'Pattern Day Trader day-trade limit',                 'src/risk/gates.py'),
    ('spec_risk.pdt_window_days',    5,   'int',   'PDT rolling window in trading days',                 'src/risk/gates.py'),
    ('spec_risk.require_conviction', 0.3, 'float', 'Minimum conviction to pass ConvictionGate',          'src/risk/gates.py')
ON CONFLICT (param_name) DO NOTHING;

-- ── Circuit Breaker defaults (src/safety.py: CircuitBreaker) ─────────────────
INSERT INTO trading.system_params (param_name, param_value, param_type, description, source_file)
VALUES
    ('circuit_breaker.drawdown.caution',   0.05, 'float', 'Drawdown % to trigger CAUTION level',          'src/safety.py'),
    ('circuit_breaker.drawdown.paused',    0.10, 'float', 'Drawdown % to trigger PAUSED level',           'src/safety.py'),
    ('circuit_breaker.drawdown.emergency', 0.15, 'float', 'Drawdown % to trigger EMERGENCY level',        'src/safety.py'),
    ('circuit_breaker.consecutive_losses_max', 3, 'int', 'Max consecutive losses before cooling off',    'src/safety.py'),
    ('circuit_breaker.cool_off_ticks',         2, 'int', 'Ticks to skip during cool-off period',          'src/safety.py')
ON CONFLICT (param_name) DO NOTHING;

-- ── Change Governor defaults (src/safety.py: ChangeGovernor) ─────────────────
INSERT INTO trading.system_params (param_name, param_value, param_type, description, source_file)
VALUES
    ('change_governor.max_monthly_changes', 5,   'int',   'Max parameter changes per month',              'src/safety.py'),
    ('change_governor.damping_factor',      0.3, 'float', 'Smoothing: new = (1-d)*old + d*proposed',      'src/safety.py'),
    ('change_governor.freeze_days',         5,   'int',   'Days to freeze param after acceptance',         'src/safety.py'),
    ('change_governor.revert_window_days',  20,  'int',   'Days within which a revert is detected',        'src/safety.py'),
    ('change_governor.revert_penalty',      0.5, 'float', 'Multiplier on future budget after revert',      'src/safety.py')
ON CONFLICT (param_name) DO NOTHING;

-- ── Shadow Mode defaults (src/safety.py: ShadowMode) ─────────────────────────
INSERT INTO trading.system_params (param_name, param_value, param_type, description, source_file)
VALUES
    ('shadow_mode.eval_days',             5,    'int',   'Days to run shadow before decision',             'src/safety.py'),
    ('shadow_mode.auto_merge_threshold',  0.10, 'float', 'Improvement % to auto-merge shadow config',     'src/safety.py'),
    ('shadow_mode.notify_threshold',      0.05, 'float', 'Improvement % to notify (5-10% band)',           'src/safety.py'),
    ('shadow_mode.review_threshold',      0.01, 'float', 'Improvement % to create review PR (1-5%)',       'src/safety.py'),
    ('shadow_mode.rollback_threshold',    0.10, 'float', 'Live degradation % to auto-revert',              'src/safety.py'),
    ('shadow_mode.rollback_window_days',  10,   'int',   'Days after merge to watch for degradation',      'src/safety.py')
ON CONFLICT (param_name) DO NOTHING;

-- ── Recovery defaults (src/safety.py: RecoveryManager) ───────────────────────
INSERT INTO trading.system_params (param_name, param_value, param_type, description, source_file)
VALUES
    ('recovery.min_recovery_ticks', 10, 'int', 'Minimum ticks to hold in recovery mode', 'src/safety.py')
ON CONFLICT (param_name) DO NOTHING;

-- ── Knowledge / Signal Board defaults (src/knowledge.py) ─────────────────────
INSERT INTO trading.system_params (param_name, param_value, param_type, description, source_file)
VALUES
    ('knowledge.signal_board.max_signals',          500, 'int',   'Max cached signals on shared board',     'src/knowledge.py'),
    ('knowledge.signal_board.max_age_hours',         24, 'int',   'Max age in hours before signal expires',  'src/knowledge.py'),
    ('knowledge.tool_profile.unused_days_before_revoke', 30, 'int', 'Days before revoking unused tool access', 'src/knowledge.py')
ON CONFLICT (param_name) DO NOTHING;

-- ── Parameter History defaults (src/param_history.py) ────────────────────────
INSERT INTO trading.system_params (param_name, param_value, param_type, description, source_file)
VALUES
    ('param_history.convergence.min_samples',     3,  'int',   'Min samples before convergence check',       'src/param_history.py'),
    ('param_history.report.default_days',        30,  'int',   'Default lookback days for param reports',     'src/param_history.py')
ON CONFLICT (param_name) DO NOTHING;

-- ── Regime Detector defaults (src/regime_detector.py, src/train_regime_detector.py) ─
INSERT INTO trading.system_params (param_name, param_value, param_type, description, source_file)
VALUES
    ('regime.kmeans.n_clusters',      4,   'int', 'Number of K-Means clusters for regime detection',    'src/regime_detector.py'),
    ('regime.training.history_days', 506,  'int', 'Days of market history for initial K-Means training', 'src/train_regime_detector.py')
ON CONFLICT (param_name) DO NOTHING;

-- ── Agent crossover prevention (src/knowledge.py) ────────────────────────────
INSERT INTO trading.system_params (param_name, param_value, param_type, description, source_file)
VALUES
    ('cross_trader.consensus_threshold',    3,    'int',   'Min traders needed for consensus signal',      'src/knowledge.py'),
    ('cross_trader.max_overlap_pct',        0.50, 'float', 'Max position overlap % before herding warning', 'src/knowledge.py')
ON CONFLICT (param_name) DO NOTHING;

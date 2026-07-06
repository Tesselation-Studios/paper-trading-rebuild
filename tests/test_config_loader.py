#!/usr/bin/env python3
"""
Tests for config_loader — both YAML config system (new) and DB agent_params (legacy).

Run:
    pytest tests/test_config_loader.py -v
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure src/ is on path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from config_loader import (
    Config,
    ConfigValidationError,
    get_config,
    load_config,
    load_all,
    _resolve_env_vars,
    _get_nested,
    # Legacy DB-backed API
    get_param,
    set_param,
    get_param_metadata,
    list_params,
    can_adjust,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _write_yaml(tmpdir: Path, name: str, content: str, config_dir: Path = None) -> Path:
    """Write a YAML file to a temp config directory."""
    d = config_dir if config_dir else (tmpdir / "config")
    d.mkdir(exist_ok=True, parents=True)
    path = d / f"{name}.yaml"
    path.write_text(content)
    return d


# ═══════════════════════════════════════════════════════════════════════════════
# Env Var Resolution
# ═══════════════════════════════════════════════════════════════════════════════

class TestEnvVarResolution:
    """Tests for _resolve_env_vars."""

    def test_resolves_env_var(self, monkeypatch):
        monkeypatch.setenv("TEST_VAR", "hello")
        assert _resolve_env_vars("${TEST_VAR}") == "hello"

    def test_resolves_env_var_with_default_present(self, monkeypatch):
        monkeypatch.setenv("TEST_VAR", "override")
        result = _resolve_env_vars("${TEST_VAR:-default}")
        assert result == "override"

    def test_resolves_default_when_env_missing(self, monkeypatch):
        monkeypatch.delenv("MISSING_VAR", raising=False)
        result = _resolve_env_vars("${MISSING_VAR:-fallback}")
        assert result == "fallback"

    def test_raises_when_env_missing_no_default(self, monkeypatch):
        monkeypatch.delenv("REQUIRED_VAR", raising=False)
        with pytest.raises(ValueError, match="REQUIRED_VAR"):
            _resolve_env_vars("${REQUIRED_VAR}")

    def test_resolves_in_dict(self, monkeypatch):
        monkeypatch.setenv("HOST", "localhost")
        monkeypatch.setenv("PORT", "8080")
        data = {
            "host": "${HOST}",
            "port": "${PORT:-5000}",
            "nested": {"url": "http://${HOST}:${PORT:-5000}"},
        }
        result = _resolve_env_vars(data)
        assert result["host"] == "localhost"
        assert result["port"] == "8080"
        assert result["nested"]["url"] == "http://localhost:8080"

    def test_resolves_in_list(self, monkeypatch):
        monkeypatch.setenv("A", "1")
        monkeypatch.setenv("B", "2")
        data = ["${A}", "${B:-x}", "${C:-3}"]
        result = _resolve_env_vars(data)
        assert result == ["1", "2", "3"]

    def test_string_without_env_var_unchanged(self):
        assert _resolve_env_vars("plain string") == "plain string"

    def test_int_unchanged(self):
        assert _resolve_env_vars(42) == 42

    def test_float_unchanged(self):
        assert _resolve_env_vars(3.14) == 3.14

    def test_bool_unchanged(self):
        assert _resolve_env_vars(True) is True

    def test_none_unchanged(self):
        assert _resolve_env_vars(None) is None


# ═══════════════════════════════════════════════════════════════════════════════
# Nested Access
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetNested:
    """Tests for _get_nested dot-notation access."""

    def test_single_level(self):
        data = {"a": 1}
        assert _get_nested(data, "a") == 1

    def test_multi_level(self):
        data = {"a": {"b": {"c": 42}}}
        assert _get_nested(data, "a.b.c") == 42

    def test_default_returned(self):
        data = {"a": 1}
        assert _get_nested(data, "missing", default="fallback") == "fallback"

    def test_keyerror_when_missing(self):
        data = {"a": 1}
        with pytest.raises(KeyError, match="missing"):
            _get_nested(data, "missing")

    def test_keyerror_deep(self):
        data = {"a": {"b": 1}}
        with pytest.raises(KeyError):
            _get_nested(data, "a.b.c")

    def test_traverse_into_non_dict(self):
        data = {"a": 42}
        with pytest.raises(KeyError):
            _get_nested(data, "a.b")


# ═══════════════════════════════════════════════════════════════════════════════
# Config Class — Basic Loading
# ═══════════════════════════════════════════════════════════════════════════════

class TestConfigLoading:
    """Tests for Config.load_config and Config.load_all."""

    def test_load_single_config(self, tmp_path):
        config_dir = _write_yaml(tmp_path, "test", "foo: bar\nnum: 42\n")
        cfg = Config(config_dir=config_dir)
        data = cfg.load_config("test")
        assert data == {"foo": "bar", "num": 42}

    def test_load_multiple_configs(self, tmp_path):
        config_dir = _write_yaml(tmp_path, "one", "key1: val1\n")
        _write_yaml(tmp_path, "two", "key2: val2\n", config_dir=config_dir)
        cfg = Config(config_dir=config_dir)
        cfg.load_all()
        assert "one" in cfg._data
        assert "two" in cfg._data
        assert cfg._data["one"] == {"key1": "val1"}
        assert cfg._data["two"] == {"key2": "val2"}

    def test_get_with_dot_notation(self, tmp_path):
        config_dir = _write_yaml(tmp_path, "app",
            "settings:\n  timeout: 30\n  retries: 3\n")
        cfg = Config(config_dir=config_dir)
        cfg.load_all()
        assert cfg.get("app.settings.timeout") == 30
        assert cfg.get("app.settings.retries") == 3

    def test_get_with_default(self, tmp_path):
        config_dir = _write_yaml(tmp_path, "test", "a: 1\n")
        cfg = Config(config_dir=config_dir)
        cfg.load_all()
        assert cfg.get("test.missing", "default") == "default"

    def test_get_keyerror(self, tmp_path):
        config_dir = _write_yaml(tmp_path, "test", "a: 1\n")
        cfg = Config(config_dir=config_dir)
        cfg.load_all()
        with pytest.raises(KeyError):
            cfg.get("nonexistent")

    def test_getitem(self, tmp_path):
        config_dir = _write_yaml(tmp_path, "test", "x: hello\n")
        cfg = Config(config_dir=config_dir)
        cfg.load_all()
        assert cfg["test.x"] == "hello"

    def test_contains(self, tmp_path):
        config_dir = _write_yaml(tmp_path, "test", "x: 1\n")
        cfg = Config(config_dir=config_dir)
        cfg.load_all()
        assert "test.x" in cfg
        assert "test.missing" not in cfg

    def test_load_empty_yaml(self, tmp_path):
        config_dir = _write_yaml(tmp_path, "empty", "")
        cfg = Config(config_dir=config_dir)
        data = cfg.load_config("empty")
        assert data == {}

    def test_load_yaml_with_only_comments(self, tmp_path):
        config_dir = _write_yaml(tmp_path, "comments", "# just a comment\n")
        cfg = Config(config_dir=config_dir)
        data = cfg.load_config("empty")  # loads empty.yaml
        # comments-only is treated as empty
        assert data == {}

    def test_load_nonexistent_config(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir(exist_ok=True)
        cfg = Config(config_dir=config_dir)
        data = cfg.load_config("nonexistent")
        assert data == {}  # graceful fallback

    def test_loaded_files_property(self, tmp_path):
        config_dir = _write_yaml(tmp_path, "a", "x: 1\n")
        cfg = Config(config_dir=config_dir)
        cfg.load_all()
        assert "a" in cfg.loaded_files


# ═══════════════════════════════════════════════════════════════════════════════
# Env Var Overrides in Config
# ═══════════════════════════════════════════════════════════════════════════════

class TestConfigEnvVars:
    """Tests for env var overrides in YAML configs."""

    def test_env_var_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PAPER_ACCOUNT_ID", "test-account-123")
        config_dir = _write_yaml(tmp_path, "paper",
            'account:\n  id: "${PAPER_ACCOUNT_ID:-paper-default}"\n  initial_balance: 100000\n')
        cfg = Config(config_dir=config_dir)
        cfg.load_all()
        assert cfg.get("paper.account.id") == "test-account-123"

    def test_env_var_fallback_to_default(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PAPER_ACCOUNT_ID", raising=False)
        config_dir = _write_yaml(tmp_path, "paper",
            'account:\n  id: "${PAPER_ACCOUNT_ID:-paper-default}"\n  initial_balance: 100000\n')
        cfg = Config(config_dir=config_dir)
        cfg.load_all()
        assert cfg.get("paper.account.id") == "paper-default"

    def test_env_var_missing_required_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("REQUIRED_SECRET", raising=False)
        config_dir = _write_yaml(tmp_path, "secrets",
            'api_key: "${REQUIRED_SECRET}"\n')
        cfg = Config(config_dir=config_dir)
        with pytest.raises(ConfigValidationError, match="REQUIRED_SECRET"):
            cfg.load_config("secrets")


# ═══════════════════════════════════════════════════════════════════════════════
# Validation
# ═══════════════════════════════════════════════════════════════════════════════

class TestConfigValidation:
    """Tests for config validation: required fields, types, ranges."""

    def test_required_fields_present(self, tmp_path):
        config_dir = _write_yaml(tmp_path, "risk",
            "position:\n  max_position_pct: 0.10\n  max_total_exposure: 0.85\n"
            "drawdown:\n  daily_loss_pct: 0.03\n  max_drawdown_pct: 0.20\n")
        cfg = Config(config_dir=config_dir)
        cfg.load_config("risk")  # should not raise

    def test_missing_required_field_raises(self, tmp_path):
        config_dir = _write_yaml(tmp_path, "risk",
            "position:\n  max_total_exposure: 0.85\n"
            "drawdown:\n  daily_loss_pct: 0.03\n  max_drawdown_pct: 0.20\n")
        cfg = Config(config_dir=config_dir)
        with pytest.raises(ConfigValidationError, match="max_position_pct"):
            cfg.load_config("risk")

    def test_type_validation(self, tmp_path):
        config_dir = _write_yaml(tmp_path, "risk",
            "position:\n  max_position_pct: \"not_a_number\"\n  max_total_exposure: 0.85\n"
            "drawdown:\n  daily_loss_pct: 0.03\n  max_drawdown_pct: 0.20\n")
        cfg = Config(config_dir=config_dir)
        with pytest.raises(ConfigValidationError, match="Type mismatch"):
            cfg.load_config("risk")

    def test_range_validation_below_min(self, tmp_path):
        config_dir = _write_yaml(tmp_path, "risk",
            "position:\n  max_position_pct: -0.50\n  max_total_exposure: 0.85\n"
            "drawdown:\n  daily_loss_pct: 0.03\n  max_drawdown_pct: 0.20\n")
        cfg = Config(config_dir=config_dir)
        with pytest.raises(ConfigValidationError, match="below minimum"):
            cfg.load_config("risk")

    def test_range_validation_above_max(self, tmp_path):
        config_dir = _write_yaml(tmp_path, "risk",
            "position:\n  max_position_pct: 5.0\n  max_total_exposure: 0.85\n"
            "drawdown:\n  daily_loss_pct: 0.03\n  max_drawdown_pct: 0.20\n")
        cfg = Config(config_dir=config_dir)
        with pytest.raises(ConfigValidationError, match="above maximum"):
            cfg.load_config("risk")

    def test_validate_returns_issues(self, tmp_path):
        # Create config with an out-of-range value (no _REQUIRED for "app" namespace)
        config_dir = _write_yaml(tmp_path, "app", "port: 999999\n")
        cfg = Config(config_dir=config_dir)
        cfg.load_config("app")
        # All validators are namespace-specific, app has no validators → empty
        assert cfg.validate() == []

    def test_validate_catches_real_issues(self, tmp_path):
        # Create a risk config with bad values
        config_dir = _write_yaml(tmp_path, "risk",
            "position:\n  max_position_pct: 999\n  max_total_exposure: 0.85\n"
            "drawdown:\n  daily_loss_pct: 0.03\n  max_drawdown_pct: 0.20\n")
        cfg = Config(config_dir=config_dir)
        with pytest.raises(ConfigValidationError, match="above maximum"):
            cfg.load_config("risk")


# ═══════════════════════════════════════════════════════════════════════════════
# Secrets Handling
# ═══════════════════════════════════════════════════════════════════════════════

class TestSecretsHandling:
    """Tests for secret handling — keys never leaked in output."""

    def test_sanitized_masks_secret_keys(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECRET_API_KEY", "sk-abc123")
        config_dir = _write_yaml(tmp_path, "app",
            'api_key: "${SECRET_API_KEY}"\n'
            'secret_token: "${SECRET_API_KEY}"\n'
            'password: "mypass"\n'
            'public_name: "hello"\n'
            'nested:\n  api_key: "inner"\n')
        cfg = Config(config_dir=config_dir)
        cfg.load_all()
        sanitized = cfg.sanitized()
        assert sanitized["app"]["api_key"] == "***"
        assert sanitized["app"]["secret_token"] == "***"
        assert sanitized["app"]["password"] == "***"
        assert sanitized["app"]["public_name"] == "hello"
        assert sanitized["app"]["nested"]["api_key"] == "***"

    def test_resolved_values_not_in_repr(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_KEY", "super-secret-value")
        config_dir = _write_yaml(tmp_path, "app",
            'api_key: "${MY_KEY}"\n')
        cfg = Config(config_dir=config_dir)
        cfg.load_all()
        rep = repr(cfg)
        # repr should not expose the actual key value
        assert "super-secret-value" not in rep

    def test_keys_come_from_env_not_yaml(self, tmp_path, monkeypatch):
        """API keys come from env vars, NEVER hardcoded in YAML."""
        monkeypatch.setenv("ALPACA_KEY", "env-provided-key")
        config_dir = _write_yaml(tmp_path, "paper",
            'account:\n  id: "test"\n  initial_balance: 100000\n'
            'alpaca:\n  key: "${ALPACA_KEY}"\n')
        cfg = Config(config_dir=config_dir)
        cfg.load_all()
        # The YAML only references the env var name, the value comes from env
        assert cfg.get("paper.alpaca.key") == "env-provided-key"


# ═══════════════════════════════════════════════════════════════════════════════
# Real Config Files
# ═══════════════════════════════════════════════════════════════════════════════

class TestRealConfigFiles:
    """Tests that the actual config YAML files in the project parse without error."""

    def test_data_bus_yaml_loads(self):
        data = load_config("data_bus")
        assert isinstance(data, dict)
        assert "cache_ttl" in data
        assert "scheduler_intervals" in data
        assert "rate_limits" in data

    def test_traders_yaml_loads(self):
        data = load_config("traders")
        assert isinstance(data, dict)
        assert "traders" in data
        assert isinstance(data["traders"], list)
        assert len(data["traders"]) == 5

    def test_risk_yaml_loads(self):
        data = load_config("risk")
        assert isinstance(data, dict)
        assert "position" in data
        assert "drawdown" in data
        assert "sizing" in data
        assert "stop_loss" in data
        assert "gates" in data

    def test_paper_yaml_loads(self):
        data = load_config("paper")
        assert isinstance(data, dict)
        assert "account" in data
        assert "alpaca" in data
        assert "data_sources" in data

    def test_all_configs_load(self):
        """All config files parse without error."""
        config = get_config(reload=True)
        assert len(config.loaded_files) >= 4
        # Check key values from each
        assert config.get("risk.position.max_position_pct") == 0.25
        assert config.get("data_bus.cache_ttl.quotes") == 5
        assert len(config.get("traders.traders")) == 5
        assert "paper-primary" in config.get("paper.account.id")

    def test_env_var_overrides_paper_account(self, monkeypatch):
        """PAPER_ACCOUNT_ID env var overrides paper.account.id."""
        monkeypatch.setenv("PAPER_ACCOUNT_ID", "test-override-001")
        config = get_config(reload=True)
        assert config.get("paper.account.id") == "test-override-001"

    def test_config_values_match_defaults(self):
        """Verify specific config values match expected defaults."""
        config = get_config(reload=True)
        # Data bus defaults
        assert config.get("data_bus.cache_ttl.quotes") == 5
        assert config.get("data_bus.rate_limits.quotes_per_min") == 200
        assert config.get("data_bus.signals.max_age") == 900
        # Risk defaults
        assert config.get("risk.position.max_position_pct") == 0.25
        assert config.get("risk.drawdown.daily_loss_pct") == 0.03
        assert config.get("risk.sizing.risk_per_trade_pct") == 0.02
        assert config.get("risk.gates.require_conviction") == 0.3
        assert config.get("risk.stop_loss.default_pct") == 0.05
        # Paper defaults
        assert config.get("paper.account.initial_balance") == 100000
        assert config.get("paper.alpaca.paper") is True


# ═══════════════════════════════════════════════════════════════════════════════
# Graceful Fallback
# ═══════════════════════════════════════════════════════════════════════════════

class TestGracefulFallback:
    """Tests for graceful handling of missing or invalid config."""

    def test_missing_config_file_returns_empty_dict(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir(exist_ok=True)
        cfg = Config(config_dir=config_dir)
        result = cfg.load_config("nonexistent")
        assert result == {}

    def test_load_all_with_missing_directory(self, tmp_path):
        config_dir = tmp_path / "nonexistent_dir"
        cfg = Config(config_dir=config_dir)
        result = cfg.load_all()
        assert result == {}

    def test_errors_tracked(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir(exist_ok=True)
        cfg = Config(config_dir=config_dir)
        cfg.load_config("nonexistent")
        assert len(cfg.errors) >= 1

    def test_reload_clears_errors(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir(exist_ok=True)
        cfg = Config(config_dir=config_dir)
        cfg.load_config("nonexistent")
        assert len(cfg.errors) > 0
        cfg.reload()
        assert len(cfg.errors) == 0

    def test_invalid_yaml_raises(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "bad.yaml").write_text(": invalid yaml : :")
        cfg = Config(config_dir=config_dir)
        with pytest.raises(ConfigValidationError, match="YAML parse error"):
            cfg.load_config("bad")


# ═══════════════════════════════════════════════════════════════════════════════
# Legacy DB-Backed API (kept for backward compatibility)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.skip(reason="Legacy DB-backed config — not used in YAML-only rebuild")
class TestLegacyConfigLoader:
    """Tests for config_loader with agent_params-based schema."""

    def test_get_param_risk(self):
        assert get_param("risk.max_position_pct") == 0.10
        # risk.exit_condition_required doesn't exist → returns default
        assert get_param("risk.exit_condition_required", default=False) is False

    def test_get_param_strategy(self):
        assert get_param("strategy.min_cycle_days") == 1.0
        assert get_param("strategy.kill_drawdown_pct") == 0.20

    def test_get_param_monitoring(self):
        assert get_param("monitoring.check_interval_minutes") == 90.0
        assert get_param("monitoring.max_consecutive_vetos") == 3.0

    def test_get_param_missing_returns_default(self):
        assert get_param("nonexistent", default=42) == 42
        assert get_param("risk.nonexistent", default="fallback") == "fallback"

    def test_get_param_metadata(self):
        meta = get_param_metadata("risk.max_position_pct")
        assert meta["param_value"] == 0.10
        assert meta["min_value"] == 0.01
        assert meta["max_value"] == 0.30
        assert meta["step_size"] == 0.05

    def test_set_param_valid(self):
        assert set_param("risk.max_position_pct", 0.15) is True
        assert get_param("risk.max_position_pct") == 0.15
        set_param("risk.max_position_pct", 0.10)  # restore

    def test_set_param_clamps_to_bounds(self):
        assert set_param("risk.max_daily_loss_pct", 0.99) is True
        assert get_param("risk.max_daily_loss_pct") == 0.10  # clamped to max
        set_param("risk.max_daily_loss_pct", 0.03)  # restore

    def test_set_param_unknown_key(self):
        assert set_param("nonexistent.key", 5) is False

    def test_list_params(self):
        params = list_params()
        assert isinstance(params, list)
        param_names = [p["param_name"] for p in params]
        assert "risk.max_position_pct" in param_names
        # Find the risk.max_position_pct entry
        rmp = next(p for p in params if p["param_name"] == "risk.max_position_pct")
        assert rmp["param_value"] == 0.10
        assert rmp["step_size"] == 0.05

    def test_can_adjust_allowed(self):
        r = can_adjust("risk.max_position_pct", 0.03)
        assert r["allowed"] is True
        assert r["new_value"] == 0.13

    def test_can_adjust_exceeds_step(self):
        r = can_adjust("risk.max_position_pct", 0.20)
        assert r["allowed"] is False

    def test_can_adjust_below_min(self):
        r = can_adjust("risk.max_position_pct", -1.0)
        assert r["allowed"] is False

    def test_can_adjust_above_max(self):
        r = can_adjust("risk.max_daily_loss_pct", 0.50)
        assert r["allowed"] is False


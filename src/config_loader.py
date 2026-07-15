#!/usr/bin/env python3
"""
Configuration Loader — unified config system.

Two layers:
  1. YAML Config System (NEW) — loads YAML files from config/ directory,
     resolves ${ENV_VAR} references, supports dot-notation access.
     No DB dependency. Pure YAML + env.

  2. Agent Params (LEGACY) — DB-backed parameter store in shared/trader.db.
     Kept for backward compatibility with param_optimizer, strategy_lifecycle,
     benchmark_tracker.

Usage (new YAML system):
    from src.config_loader import load_config, load_all, Config

    config = Config()
    ttl = config.get("data_bus.cache_ttl.quotes")          # → 5
    max_pos = config.get("risk.position.max_position_pct")  # → 0.10

    # Or with top-level helpers:
    cfg = load_config("risk")  # → dict for risk.yaml
    all_cfg = load_all()       # → merged dict of all configs

    # Env var override: set PAPER_ACCOUNT_ID to override paper.account.id

Usage (legacy DB):
    from src.config_loader import get_param, set_param, list_params
    max_pos = get_param("risk.max_position_pct")
"""

import os
import re
import json
import logging
import sqlite3
import argparse
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union
from copy import deepcopy

log = logging.getLogger("config_loader")

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_DIR / "config"
DB_PATH = PROJECT_DIR / "shared" / "trader.db"

# ═══════════════════════════════════════════════════════════════════════════════
# YAML Config System (NEW)
# ═══════════════════════════════════════════════════════════════════════════════

# Regex for ${ENV_VAR} and ${ENV_VAR:-default} patterns
_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _coerce_type(value: Any, original: Any) -> Any:
    """Coerce a resolved string value to match the original YAML type.

    If original was an int/float/bool but the resolved value is a string
    (e.g., from ${ENV_VAR:-100000}), try to convert it back.
    """
    if not isinstance(value, str):
        return value
    if isinstance(original, bool):
        return value.lower() in ("true", "1", "yes")
    if isinstance(original, int):
        try:
            return int(value)
        except (ValueError, TypeError):
            return value
    if isinstance(original, float):
        try:
            return float(value)
        except (ValueError, TypeError):
            return value
    return value


def _resolve_env_vars(value: Any, original: Any = None) -> Any:
    """Recursively resolve ${ENV_VAR} and ${ENV_VAR:-default} in a value.

    Args:
        value: String, dict, list, or scalar.
        original: Original value (for type coercion after resolution).

    Returns:
        Value with all env var references resolved.

    Raises:
        ValueError: If a required env var (no default) is not set.
    """
    if isinstance(value, str):
        def _replace(match):
            var_name = match.group(1)
            default = match.group(2)
            env_val = os.getenv(var_name)
            if env_val is not None:
                return env_val
            if default is not None:
                return default
            raise ValueError(
                f"Required environment variable '{var_name}' is not set "
                f"(no default provided). Set it or add a default in the YAML config."
            )
        result = _ENV_VAR_RE.sub(_replace, value)
        # Coerce type based on original value
        if original is not None:
            result = _coerce_type(result, original)
        return result
    elif isinstance(value, dict):
        if original is None:
            original = value
        return {k: _resolve_env_vars(v, original.get(k) if isinstance(original, dict) else None)
                for k, v in value.items()}
    elif isinstance(value, list):
        return [_resolve_env_vars(item, original[i] if original and i < len(original) else None)
                for i, item in enumerate(value)]
    return value


def _merge_dicts(base: dict, override: dict) -> dict:
    """Deep merge override into base. Lists are replaced, not merged."""
    result = deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _merge_dicts(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


_sentinel = object()


def _check_type(value: Any, expected_type: type) -> bool:
    """Check if value matches expected_type, with lenient int/float handling.

    YAML parses '35' as int, but our validators may expect float.
    This function accepts int for float fields as a valid match.
    """
    if isinstance(value, expected_type):
        return True
    if expected_type is float and isinstance(value, int):
        return True
    return False


def _get_nested(data: dict, key: str, default: Any = _sentinel) -> Any:
    """Get a nested value using dot notation: 'risk.position.max_position_pct'.

    Args:
        data: The dict to traverse.
        key: Dot-separated path.
        default: Value to return if key not found.

    Returns:
        The value at the given path.

    Raises:
        KeyError: If default is not provided and key not found.
    """
    keys = key.split(".")
    current = data
    for i, k in enumerate(keys):
        if not isinstance(current, dict):
            if default is not _sentinel:
                return default
            raise KeyError(f"Cannot traverse into non-dict at '{'.'.join(keys[:i])}' for key '{key}'")
        if k not in current:
            if default is not _sentinel:
                return default
            raise KeyError(f"Key '{key}' not found (missing '{k}' at '{'.'.join(keys[:i]) or 'root'}')")
        current = current[k]
    return current



class ConfigValidationError(Exception):
    """Raised when config validation fails."""
    pass


class Config:
    """YAML-based configuration loader with env var resolution.

    Loads all YAML files from config/ directory, resolves ${ENV_VAR} references,
    and provides dot-notation access to nested values.

    Usage:
        config = Config()
        ttl = config.get("data_bus.cache_ttl.quotes")  # → 5

        # Access full namespace:
        risk = config.get("risk")  # → dict from risk.yaml
    """

    # Required keys per config file (config_name → [required dotted paths])
    _REQUIRED = {
        "risk": [
            "position.max_position_pct",
            "position.max_total_exposure",
            "drawdown.daily_loss_pct",
            "drawdown.max_drawdown_pct",
        ],
        "paper": [
            "account.id",
            "account.initial_balance",
        ],
    }

    # Type/range validation specs (dotted_path → (expected_type, min, max))
    _VALIDATORS = {
        "risk.position.max_position_pct": (float, 0.0, 1.0),
        "risk.position.max_sector_pct": (float, 0.0, 1.0),
        "risk.position.max_total_exposure": (float, 0.0, 1.0),
        "risk.position.max_positions": (int, 1, 100),
        "risk.drawdown.daily_loss_pct": (float, 0.0, 1.0),
        "risk.drawdown.weekly_loss_pct": (float, 0.0, 1.0),
        "risk.drawdown.max_drawdown_pct": (float, 0.0, 1.0),
        "risk.sizing.risk_per_trade_pct": (float, 0.0, 0.5),
        "risk.sizing.min_position_value": (int, 0, 1_000_000),
        "risk.sizing.max_position_value": (int, 0, 1_000_000),
        "risk.stop_loss.default_pct": (float, 0.0, 1.0),
        "risk.stop_loss.trailing_pct": (float, 0.0, 1.0),
        "risk.stop_loss.atr_multiplier": (float, 0.1, 10.0),
        "risk.stop_loss.profit_target_pct": (float, 0.0, 10.0),
        "risk.gates.require_conviction": (float, 0.0, 1.0),
        "risk.gates.max_correlated_bets": (int, 1, 50),
        "risk.gates.vetos_before_force_hold": (int, 1, 20),
        "risk.volatility.max_vix_threshold": (float, 10.0, 100.0),
        "risk.volatility.min_volume_rank": (float, 0.0, 1.0),
        "risk.volatility.max_spread_pct": (float, 0.0, 0.1),
        "paper.account.initial_balance": (int, 1000, 100_000_000),
        "data_bus.cache_ttl.quotes": (int, 1, 3600),
        "data_bus.rate_limits.quotes_per_min": (int, 1, 10000),
        "data_bus.signals.max_age": (int, 60, 86400),
    }

    def __init__(self, config_dir: Optional[Path] = None):
        """Initialize and load all configs.

        Args:
            config_dir: Path to config directory. Defaults to PROJECT_DIR/config.
        """
        self._config_dir = Path(config_dir) if config_dir else CONFIG_DIR
        self._data: Dict[str, Any] = {}
        self._loaded_files: List[str] = []
        self._errors: List[str] = []

    def load_config(self, name: str) -> dict:
        """Load a single YAML config file.

        Args:
            name: Config name without extension (e.g., 'data_bus', 'risk').

        Returns:
            Parsed dict with env vars resolved.

        Raises:
            FileNotFoundError: If the YAML file doesn't exist.
            ConfigValidationError: If validation fails.
        """
        yaml_path = self._config_dir / f"{name}.yaml"
        yml_path = self._config_dir / f"{name}.yml"

        path = yaml_path if yaml_path.exists() else yml_path

        if not path.exists():
            msg = f"Config file not found: {yaml_path}"
            log.warning(msg)
            self._errors.append(msg)
            return {}

        try:
            import yaml
        except ImportError:
            log.error("PyYAML not installed. Install with: pip install pyyaml")
            raise

        try:
            with open(path, "r") as f:
                raw = yaml.safe_load(f)
        except yaml.YAMLError as e:
            msg = f"YAML parse error in {path}: {e}"
            log.error(msg)
            self._errors.append(msg)
            raise ConfigValidationError(msg) from e

        if raw is None:
            log.warning("Config file is empty: %s", path)
            return {}

        if not isinstance(raw, dict):
            msg = f"Config file {path} must contain a mapping, got {type(raw).__name__}"
            log.error(msg)
            self._errors.append(msg)
            raise ConfigValidationError(msg)

        try:
            resolved = _resolve_env_vars(raw)
        except ValueError as e:
            msg = f"Env var resolution failed in {path}: {e}"
            log.error(msg)
            self._errors.append(msg)
            raise ConfigValidationError(msg) from e

        self._validate_config(name, resolved)
        self._data[name] = resolved
        self._loaded_files.append(name)
        return resolved

    def load_all(self) -> dict:
        """Load all YAML config files in the config directory.

        Returns:
            Merged dict of all configs, keyed by config name.

        Raises:
            ConfigValidationError: If any required validation fails.
        """
        if self._config_dir.exists():
            for entry in sorted(self._config_dir.iterdir()):
                if entry.suffix in (".yaml", ".yml"):
                    name = entry.stem
                    try:
                        self.load_config(name)
                    except ConfigValidationError:
                        # Already logged; continue loading other files
                        log.warning("Skipping invalid config: %s", entry.name)
                    except Exception as e:
                        log.warning("Failed to load %s: %s", entry.name, e)
                        self._errors.append(f"Failed to load {entry.name}: {e}")

        return dict(self._data)

    def get(self, key: str, default: Any = _sentinel) -> Any:
        """Get a config value using dot notation.

        Args:
            key: Dot-separated path, e.g., 'risk.position.max_position_pct'.
            default: Value to return if key not found.

        Returns:
            The config value.

        Raises:
            KeyError: If default is not provided and key not found.
        """
        return _get_nested(self._data, key, default)

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def __contains__(self, key: str) -> bool:
        try:
            self.get(key)
            return True
        except KeyError:
            return False

    def reload(self) -> "Config":
        """Reload all config files from disk."""
        self._data.clear()
        self._loaded_files.clear()
        self._errors.clear()
        self.load_all()
        return self

    @property
    def loaded_files(self) -> List[str]:
        """List of successfully loaded config names."""
        return list(self._loaded_files)

    @property
    def errors(self) -> List[str]:
        """List of non-fatal errors encountered during loading."""
        return list(self._errors)

    def validate(self) -> List[str]:
        """Run all validation checks. Returns list of error messages (empty = valid)."""
        issues = []
        for name, required_keys in self._REQUIRED.items():
            if name not in self._data:
                continue
            for key in required_keys:
                try:
                    _get_nested(self._data[name], key)
                except KeyError:
                    issues.append(f"Missing required key: {name}.{key}")

        for key, (expected_type, vmin, vmax) in self._VALIDATORS.items():
            try:
                value = self.get(key)
            except KeyError:
                continue
            if not _check_type(value, expected_type):
                issues.append(
                    f"Type mismatch for {key}: expected {expected_type.__name__}, "
                    f"got {type(value).__name__} ({value!r})"
                )
                continue
            if vmin is not None and value < vmin:
                issues.append(f"Value for {key} ({value}) is below minimum ({vmin})")
            if vmax is not None and value > vmax:
                issues.append(f"Value for {key} ({value}) is above maximum ({vmax})")

        return issues

    def _validate_config(self, name: str, data: dict) -> None:
        """Validate a single config namespace against requirements."""
        issues = []

        # Check required keys
        for key in self._REQUIRED.get(name, []):
            try:
                _get_nested(data, key)
            except KeyError:
                issues.append(f"Missing required key: {name}.{key}")

        # Check type/range validators
        for full_key, (expected_type, vmin, vmax) in self._VALIDATORS.items():
            if not full_key.startswith(f"{name}."):
                continue
            local_key = full_key[len(name) + 1:]
            try:
                value = _get_nested(data, local_key)
            except KeyError:
                continue
            if not _check_type(value, expected_type):
                issues.append(
                    f"Type mismatch for {full_key}: expected {expected_type.__name__}, "
                    f"got {type(value).__name__} ({value!r})"
                )
                continue
            if vmin is not None and value < vmin:
                issues.append(f"Value for {full_key} ({value}) is below minimum ({vmin})")
            if vmax is not None and value > vmax:
                issues.append(f"Value for {full_key} ({value}) is above maximum ({vmax})")

        if issues:
            msg = f"Config validation failed for {name}:\n  " + "\n  ".join(issues)
            log.error(msg)
            self._errors.append(msg)
            raise ConfigValidationError(msg)

    def sanitized(self) -> dict:
        """Return config data with secret env var names masked.

        Returns a deep copy where any key containing 'key' or 'secret'
        has its value replaced with '***' for safe display.
        """
        def _mask(data):
            if isinstance(data, dict):
                return {
                    k: "***" if isinstance(k, str) and (
                        "key" in k.lower() or "secret" in k.lower()
                        or "token" in k.lower() or "password" in k.lower()
                    ) else _mask(v)
                    for k, v in data.items()
                }
            elif isinstance(data, list):
                return [_mask(item) for item in data]
            return data
        return _mask(deepcopy(self._data))

    def __repr__(self) -> str:
        return f"Config(loaded={self._loaded_files}, errors={len(self._errors)})"


# ── Module-level singleton ────────────────────────────────────────────────────

_config_instance: Optional[Config] = None


def get_config(reload: bool = False) -> Config:
    """Get (or create) the global Config instance.

    Args:
        reload: If True, reload all config files from disk.

    Returns:
        The global Config instance.
    """
    global _config_instance
    if _config_instance is None or reload:
        _config_instance = Config()
        _config_instance.load_all()
    return _config_instance


def load_config(name: str) -> dict:
    """Load a single config file by name.

    Args:
        name: Config name without extension (e.g., 'risk').

    Returns:
        Parsed dict with env vars resolved.

    Raises:
        FileNotFoundError: If the YAML file doesn't exist.
        ConfigValidationError: If validation fails.
    """
    cfg = Config()
    return cfg.load_config(name)


def load_all() -> dict:
    """Load all YAML configs from the config directory.

    Returns:
        Merged dict of all configs, keyed by config name.
    """
    cfg = Config()
    return cfg.load_all()


# ═══════════════════════════════════════════════════════════════════════════════
# Agent Params — DB-Backed (LEGACY)
# ═══════════════════════════════════════════════════════════════════════════════

_DEFAULT_AGENT = "system"


def _connect(readonly: bool = False):
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    if readonly:
        conn.execute("PRAGMA query_only = ON")
    return conn


def _coerce(value: Any, value_type: Optional[str] = None) -> Any:
    """Coerce a value to its declared type."""
    if value is None:
        return value
    if value_type == "int":
        return int(float(value))
    elif value_type == "float":
        return float(value)
    elif value_type == "bool":
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes")
        return bool(value)
    elif value_type == "json":
        if isinstance(value, str):
            return json.loads(value)
        return value
    return value


def _log_change(agent_id: str, param_name: str, old_value: Any,
                new_value: Any, source: str = "manual", reason: str = ""):
    """Record parameter change in params_history table."""
    try:
        with _connect() as conn:
            conn.execute(
                """INSERT INTO params_history 
                   (agent_id, param_name, old_value, new_value, changed_at, source, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (agent_id, param_name, old_value, new_value,
                 datetime.now().isoformat(), source, reason)
            )
    except Exception as e:
        print(f"[config] Failed to log change: {e}", flush=True)


# ═══════════════════════════════════════════════════════════════════════════════
# Read (LEGACY)
# ═══════════════════════════════════════════════════════════════════════════════

def get_param(param_name: str, agent_id: str = _DEFAULT_AGENT,
              default: Any = None) -> Any:
    """Get a single parameter value from the agent_params DB table.

    Returns the typed Python value (int, float, bool, str, or JSON).
    Falls back to the YAML config if DB is unavailable or param not found.
    """
    try:
        with _connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT param_value FROM agent_params WHERE agent_id = ? AND param_name = ?",
                (agent_id, param_name)
            ).fetchone()
            if row:
                return row["param_value"]
    except Exception as e:
        log.warning("config_loader: %s", e)

    # Fallback: try YAML config
    try:
        cfg = get_config()
        return cfg.get(param_name)
    except (KeyError, Exception):
        return default


def get_param_metadata(param_name: str, agent_id: str = _DEFAULT_AGENT) -> Dict[str, Any]:
    """Get full parameter metadata: value, min, max, step_size, etc."""
    try:
        with _connect(readonly=True) as conn:
            row = conn.execute(
                """SELECT param_name, param_value, min_value, max_value, step_size,
                          description, updated_at, source
                   FROM agent_params WHERE agent_id = ? AND param_name = ?""",
                (agent_id, param_name)
            ).fetchone()
            if not row:
                return {}
            return dict(row)
    except Exception:
        return {}


def list_params(agent_id: str = _DEFAULT_AGENT,
                prefix: Optional[str] = None) -> List[Dict[str, Any]]:
    """List all parameters for an agent, optionally filtered by prefix."""
    try:
        with _connect(readonly=True) as conn:
            if prefix:
                rows = conn.execute(
                    "SELECT * FROM agent_params WHERE agent_id = ? AND param_name LIKE ? ORDER BY param_name",
                    (agent_id, f"{prefix}%")
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM agent_params WHERE agent_id = ? ORDER BY param_name",
                    (agent_id,)
                ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# Write (LEGACY)
# ═══════════════════════════════════════════════════════════════════════════════

def set_param(param_name: str, value: Any, agent_id: str = _DEFAULT_AGENT,
              source: str = "manual", reason: str = "") -> bool:
    """Set a parameter value, enforcing step_size and min/max bounds."""
    try:
        with _connect() as conn:
            row = conn.execute(
                """SELECT param_value, min_value, max_value, step_size
                   FROM agent_params WHERE agent_id = ? AND param_name = ?""",
                (agent_id, param_name)
            ).fetchone()
            if not row:
                print(f"[config] Unknown parameter: {param_name} (agent={agent_id})", flush=True)
                return False

            old_value = row["param_value"]
            min_val = row["min_value"]
            max_val = row["max_value"]
            step_size = row["step_size"]

            if isinstance(value, bool):
                new_value = float(value)
            else:
                new_value = float(value)

            if min_val is not None:
                new_value = max(min_val, new_value)
            if max_val is not None:
                new_value = min(max_val, new_value)

            if step_size is not None and old_value is not None and old_value != 0:
                diff = abs(new_value - old_value)
                if diff > step_size:
                    print(
                        f"[config] WARNING: {param_name} change ({old_value} → {new_value}) "
                        f"exceeds step_size ({step_size}). Applying anyway.",
                        flush=True
                    )

            conn.execute(
                """UPDATE agent_params 
                   SET param_value = ?, updated_at = ?, source = ?
                   WHERE agent_id = ? AND param_name = ?""",
                (new_value, datetime.now().isoformat(), source, agent_id, param_name)
            )

            conn.execute(
                """INSERT INTO params_history 
                   (agent_id, param_name, old_value, new_value, changed_at, source, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (agent_id, param_name, old_value, new_value,
                 datetime.now().isoformat(), source, reason)
            )

            return True

    except Exception as e:
        print(f"[config] Error setting {param_name}: {e}", flush=True)
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# Validation (LEGACY)
# ═══════════════════════════════════════════════════════════════════════════════

def can_adjust(param_name: str, delta: float,
               agent_id: str = _DEFAULT_AGENT) -> Dict[str, Any]:
    """Check whether a proposed parameter adjustment is valid."""
    meta = get_param_metadata(param_name, agent_id=agent_id)
    if not meta:
        return {"allowed": False, "new_value": 0, "reason": f"Unknown parameter: {param_name}"}

    current = float(meta.get("param_value", 0))
    step_size = meta.get("step_size")
    min_val = meta.get("min_value")
    max_val = meta.get("max_value")

    proposed = current + delta

    if step_size is not None and abs(delta) > step_size:
        return {
            "allowed": False,
            "new_value": current,
            "reason": f"Delta {delta} exceeds step_size {step_size}"
        }

    if min_val is not None and proposed < min_val:
        return {
            "allowed": False,
            "new_value": proposed,
            "reason": f"Value {proposed} below min {min_val}"
        }

    if max_val is not None and proposed > max_val:
        return {
            "allowed": False,
            "new_value": proposed,
            "reason": f"Value {proposed} above max {max_val}"
        }

    return {"allowed": True, "new_value": proposed, "reason": ""}


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Config loader — read/write agent_params")
    sub = parser.add_subparsers(dest="cmd")

    get_p = sub.add_parser("get", help="Get a param value")
    get_p.add_argument("param", help="Param name (e.g. timescale.grade_interval_ticks)")
    get_p.add_argument("--agent", default=_DEFAULT_AGENT, help="Agent ID")

    set_p = sub.add_parser("set", help="Set a param value")
    set_p.add_argument("param", help="Param name")
    set_p.add_argument("value", type=float, help="New value")
    set_p.add_argument("--agent", default=_DEFAULT_AGENT, help="Agent ID")
    set_p.add_argument("--reason", default="", help="Reason for change")

    list_p = sub.add_parser("list", help="List params")
    list_p.add_argument("--agent", default=_DEFAULT_AGENT, help="Agent ID")
    list_p.add_argument("--prefix", default=None, help="Filter by prefix")

    meta_p = sub.add_parser("meta", help="Get full metadata")
    meta_p.add_argument("param", help="Param name")
    meta_p.add_argument("--agent", default=_DEFAULT_AGENT, help="Agent ID")

    # New YAML subcommands
    yaml_p = sub.add_parser("yaml-get", help="Get a value from YAML configs")
    yaml_p.add_argument("key", help="Dot-separated key (e.g. risk.position.max_position_pct)")

    yaml_list = sub.add_parser("yaml-list", help="List loaded YAML configs")
    yaml_validate = sub.add_parser("yaml-validate", help="Validate all YAML configs")

    args = parser.parse_args()

    if args.cmd == "get":
        print(get_param(args.param, agent_id=args.agent))
    elif args.cmd == "set":
        ok = set_param(args.param, args.value, agent_id=args.agent, reason=args.reason)
        print("OK" if ok else "FAILED")
        return 0 if ok else 1
    elif args.cmd == "list":
        for p in list_params(agent_id=args.agent, prefix=args.prefix):
            print(f"  {p['param_name']:45s} = {p['param_value']}")
    elif args.cmd == "meta":
        import pprint
        pprint.pprint(get_param_metadata(args.param, agent_id=args.agent))
    elif args.cmd == "yaml-get":
        config = get_config()
        try:
            print(config.get(args.key))
        except KeyError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
    elif args.cmd == "yaml-list":
        config = get_config()
        for name in config.loaded_files:
            print(f"  {name}")
        if config.errors:
            print("\nErrors:")
            for err in config.errors:
                print(f"  ⚠ {err}")
    elif args.cmd == "yaml-validate":
        config = get_config()
        issues = config.validate()
        if issues:
            print(f"❌ {len(issues)} validation issues:")
            for issue in issues:
                print(f"  • {issue}")
            return 1
        else:
            print("✅ All configs valid")
    else:
        parser.print_help()

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

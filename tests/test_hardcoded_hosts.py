"""
Hardcoded Host Regression Tests — ensure no hardcoded IPs or old hostnames
remain in database connection strings across the codebase.

Run: PYTHONPATH=. python3 -m pytest tests/test_hardcoded_hosts.py -v
"""

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Hardcoded patterns that should never appear in DSN fallbacks
FORBIDDEN_HOSTS = ["docker.klo", "192.168.1.179"]

# env_var → (file, expected_fallback_substring)
FALLBACK_CHECKS = [
    ("REFLECTION_DB_DSN", "src/reflection_cron.py", "trading-db"),
    ("PG_DSN",            "src/pg_dashboard.py",    "trading-db"),
    ("PG_DSN",            "src/sync_exits_pg.py",   "trading-db"),
    ("VT_DB_DSN",         "src/virtual_rotate.py",  "trading-db"),
    ("VT_DB_DSN",         "src/virtual_runner.py",  "trading-db"),
    ("VT_DB_DSN",         "src/virtual_cull.py",    "trading-db"),
    ("DB_HOST",           "src/generate_tick.py",   "trading-db"),
]

# ═══════════════════════════════════════════════════════════════
# Utility: extract fallback string from os.getenv calls
# ═══════════════════════════════════════════════════════════════

def _extract_getenv_fallback(src: str, env_var: str) -> str | None:
    """
    Extract the fallback string from an os.getenv(env_var, ...) call.
    Handles both single-line and multi-line patterns.
    """
    # Try single-line: os.getenv("VAR", "fallback")
    m = re.search(
        rf'os\.getenv\(\s*"{env_var}"\s*,\s*"([^"]*)"\s*\)',
        src
    )
    if m:
        return m.group(1)

    # Try multi-line: os.getenv(\n    "VAR",\n    "fallback",\n)
    m = re.search(
        rf'os\.getenv\(\s*\n\s*"{env_var}"\s*,\s*\n\s*"([^"]*)"',
        src
    )
    if m:
        return m.group(1)

    return None


# ═══════════════════════════════════════════════════════════════
# Tests: Individual fallback values
# ═══════════════════════════════════════════════════════════════

def test_reflection_cron_fallback():
    """REFLECTION_DB_DSN fallback uses trading-db, not docker.klo."""
    src = (ROOT / "src/reflection_cron.py").read_text()
    fallback = _extract_getenv_fallback(src, "REFLECTION_DB_DSN")
    assert fallback is not None, "REFLECTION_DB_DSN os.getenv pattern not found"
    assert "trading-db" in fallback, f"Fallback should reference trading-db, got: {fallback}"
    for bad in FORBIDDEN_HOSTS:
        assert bad not in fallback, f"Fallback should not contain '{bad}', got: {fallback}"


def test_pg_dashboard_fallback():
    """PG_DSN fallback uses trading-db, not 192.168.1.179."""
    src = (ROOT / "src/pg_dashboard.py").read_text()
    fallback = _extract_getenv_fallback(src, "PG_DSN")
    assert fallback is not None, "PG_DSN os.getenv pattern not found"
    assert "trading-db" in fallback
    for bad in FORBIDDEN_HOSTS:
        assert bad not in fallback


def test_sync_exits_pg_fallback():
    """PG_DSN fallback uses trading-db, not 192.168.1.179."""
    src = (ROOT / "src/sync_exits_pg.py").read_text()
    fallback = _extract_getenv_fallback(src, "PG_DSN")
    assert fallback is not None, "PG_DSN os.getenv pattern not found"
    assert "trading-db" in fallback
    for bad in FORBIDDEN_HOSTS:
        assert bad not in fallback


def test_virtual_rotate_fallback():
    """VT_DB_DSN fallback uses trading-db, not docker.klo."""
    src = (ROOT / "src/virtual_rotate.py").read_text()
    fallback = _extract_getenv_fallback(src, "VT_DB_DSN")
    assert fallback is not None, "VT_DB_DSN os.getenv pattern not found"
    assert "trading-db" in fallback
    for bad in FORBIDDEN_HOSTS:
        assert bad not in fallback


def test_virtual_runner_fallback():
    """VT_DB_DSN fallback uses trading-db, not 192.168.1.179."""
    src = (ROOT / "src/virtual_runner.py").read_text()
    fallback = _extract_getenv_fallback(src, "VT_DB_DSN")
    assert fallback is not None, "VT_DB_DSN os.getenv pattern not found"
    assert "trading-db" in fallback
    for bad in FORBIDDEN_HOSTS:
        assert bad not in fallback


def test_virtual_cull_fallback():
    """VT_DB_DSN fallback uses trading-db, not docker.klo."""
    src = (ROOT / "src/virtual_cull.py").read_text()
    fallback = _extract_getenv_fallback(src, "VT_DB_DSN")
    assert fallback is not None, "VT_DB_DSN os.getenv pattern not found"
    assert "trading-db" in fallback
    for bad in FORBIDDEN_HOSTS:
        assert bad not in fallback


def test_generate_tick_fallback():
    """DB_HOST fallback uses 'trading-db', not '192.168.1.179'."""
    src = (ROOT / "src/generate_tick.py").read_text()
    fallback = _extract_getenv_fallback(src, "DB_HOST")
    assert fallback is not None, "DB_HOST os.getenv pattern not found"
    assert fallback == "trading-db", f"Should be 'trading-db', got: '{fallback}'"
    assert "192.168.1.179" not in fallback


# ═══════════════════════════════════════════════════════════════
# Tests: No hardcoded IPs remain in any DSN-like fallback
# ═══════════════════════════════════════════════════════════════

def test_no_hardcoded_ips_in_dsn_fallbacks():
    """
    Verify that no os.getenv DSN fallback across src/ contains
    forbidden hardcoded hosts (docker.klo, 192.168.1.179).
    """
    for pyfile in sorted(ROOT.glob("src/**/*.py")):
        src = pyfile.read_text()
        # Find all os.getenv(SOME_VAR, "string") pattern — single line
        dsn_fallbacks = re.findall(
            r'os\.getenv\(\s*[^,]+,\s*"([^"]*)"\s*\)',
            src
        )
        for fallback in dsn_fallbacks:
            if "host=" in fallback or "port=" in fallback or "dbname=" in fallback:
                for bad in FORBIDDEN_HOSTS:
                    assert bad not in fallback, (
                        f"{pyfile.name}: DSN fallback still contains "
                        f"'{bad}': {fallback}"
                    )


def test_no_hardcoded_db_host_in_connect():
    """
    Verify that no psycopg2.connect() call uses a hardcoded
    host string (bypassing os.getenv).
    """
    for pyfile in sorted(ROOT.glob("src/**/*.py")):
        src = pyfile.read_text()

        # Find psycopg2.connect( calls and check for hardcoded host=
        connect_calls = re.findall(
            r'psycopg2\.connect\([^)]*host\s*=\s*["\']([^"\']+)["\']',
            src
        )
        for host_val in connect_calls:
            # Check if it's wrapped in os.getenv — skip those
            # We already checked inline os.getenv separately
            assert host_val.startswith("trading") or host_val == "localhost", (
                f"{pyfile.name}: psycopg2.connect() has hardcoded host='{host_val}' "
                f"not using os.getenv"
            )


# ═══════════════════════════════════════════════════════════════
# Tests: Env var overrides fallback correctly
# ═══════════════════════════════════════════════════════════════

from unittest.mock import patch


@patch.dict(os.environ, {"REFLECTION_DB_DSN": "host=my-custom-db port=9999 dbname=test user=test"})
def test_reflection_cron_env_override():
    """Setting REFLECTION_DB_DSN env var overrides the fallback."""
    import importlib
    for mod_name in list(sys.modules.keys()):
        if "reflection_cron" in mod_name:
            del sys.modules[mod_name]
    from src.reflection_cron import _DB_DSN
    assert "my-custom-db" in _DB_DSN, f"Env override failed: {_DB_DSN}"
    assert "9999" in _DB_DSN


@patch.dict(os.environ, {"PG_DSN": "host=custom-db port=5432 dbname=mydb user=admin"})
def test_pg_dashboard_env_override():
    """Setting PG_DSN env var overrides the fallback."""
    import importlib
    for mod_name in list(sys.modules.keys()):
        if "pg_dashboard" in mod_name:
            del sys.modules[mod_name]
    from src.pg_dashboard import PG_DSN
    assert "custom-db" in PG_DSN


@patch.dict(os.environ, {"PG_DSN": "host=custom-db port=5432 dbname=mydb user=admin"})
def test_sync_exits_pg_env_override():
    """Setting PG_DSN env var overrides the fallback in sync_exits_pg."""
    import importlib
    for mod_name in list(sys.modules.keys()):
        if "sync_exits_pg" in mod_name:
            del sys.modules[mod_name]
    from src.sync_exits_pg import PG_DSN
    assert "custom-db" in PG_DSN


@patch.dict(os.environ, {"VT_DB_DSN": "host=custom-vt-db port=5432 dbname=vtrading user=vt"})
def test_virtual_rotate_env_override():
    """Setting VT_DB_DSN env var overrides the fallback in virtual_rotate."""
    import importlib
    for mod_name in list(sys.modules.keys()):
        if "virtual_rotate" in mod_name:
            del sys.modules[mod_name]
    from src.virtual_rotate import DB_DSN
    assert "custom-vt-db" in DB_DSN


@patch.dict(os.environ, {"VT_DB_DSN": "host=custom-vt-db port=5432 dbname=vtrading user=vt"})
def test_virtual_runner_env_override():
    """Setting VT_DB_DSN env var overrides the fallback in virtual_runner config."""
    import importlib
    for mod_name in list(sys.modules.keys()):
        if "virtual_runner" in mod_name or "signals" in mod_name or "llm_engine" in mod_name or "prompt_builder" in mod_name or "replay" in mod_name:
            del sys.modules[mod_name]
    import src.virtual_runner as vr
    importlib.reload(vr)
    config_dsn = vr._config["db_dsn"]
    assert "custom-vt-db" in config_dsn


@patch.dict(os.environ, {"VT_DB_DSN": "host=custom-vt-db port=5432 dbname=vtrading user=vt"})
def test_virtual_cull_env_override():
    """Setting VT_DB_DSN env var overrides the fallback in virtual_cull."""
    import importlib
    for mod_name in list(sys.modules.keys()):
        if "virtual_cull" in mod_name or "signals" in mod_name:
            del sys.modules[mod_name]
    from src.virtual_cull import DB_DSN
    assert "custom-vt-db" in DB_DSN


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    test_fns = [fn for fn in dir() if fn.startswith("test_")]
    failures = 0
    for fn_name in sorted(test_fns):
        fn = globals()[fn_name]
        try:
            fn()
            print(f"  ✅ {fn_name}")
        except AssertionError as e:
            print(f"  ❌ {fn_name}: {e}")
            failures += 1
        except Exception as e:
            print(f"  ❌ {fn_name}: {type(e).__name__}: {e}")
            failures += 1
    print(f"\n{failures} failures")
    sys.exit(failures)
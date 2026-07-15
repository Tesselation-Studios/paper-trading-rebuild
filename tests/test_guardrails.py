#!/usr/bin/env python3
"""
System-level guardrail tests for the paper-trading system.

Tests cover:
1. Spec compliance — bankroll, strategies, journal, positions format
2. Bug regression — silent import failures, column name mismatches, orphaned JS code
3. DB schema consistency — column names match between code and DB
4. Docker image integrity — all required modules present
"""
import os
import sys
import json
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ERRORS = []

def check(name, condition, detail=""):
    if not condition:
        ERRORS.append(f"  FAIL: {name} — {detail}")

def assert_no_orphaned_js(filepath):
    """Check for orphaned top-level code in JS that references undefined variables."""
    if not filepath.exists():
        return
    with open(filepath) as f:
        src = f.read()
    
    # Find script blocks
    scripts = re.findall(r'<script[^>]*>(.*?)</script>', src, re.DOTALL)
    for i, script in enumerate(scripts):
        lines = script.split('\n')
        for j, line in enumerate(lines):
            stripped = line.strip()
            # Watch for top-level for loops or function calls referencing undefined vars
            if stripped.startswith('for ') and 'positions' in stripped and 'let' not in stripped and 'var' not in stripped and 'const' not in stripped:
                # Check if it's inside a function
                preceding = '\n'.join(lines[:j])
                depth = preceding.count('{') - preceding.count('}')
                if depth == 0:
                    check(f"Orphaned top-level for loop in script block {i}",
                          False, f"Line {j+1}: {stripped}")

def assert_no_silent_except(filepath):
    """Check for bare 'except: pass' / 'except Exception: pass' that swallow errors."""
    if not filepath.exists():
        return
    with open(filepath) as f:
        src = f.read()
    
    # Find bare except: pass patterns (but allow them in test files)
    bare_excepts = re.findall(r'except\s+(Exception|)\s*:\s*\n\s+pass', src)
    check(f"No bare except:pass in {filepath.name}",
          len(bare_excepts) == 0,
          f"Found {len(bare_excepts)} bare except:pass blocks")

def assert_column_names_match(filepath):
    """Check that SQL column names match actual DB schema.
    
    Only flags 'qty' when it appears as a SQL column reference, not as
    a JSON response key, local variable, or Alpaca object attribute.
    """
    if not filepath.exists():
        return
    with open(filepath) as f:
        src = f.read()
    
    # Check for likely wrong column names in SQL queries
    # Common pandas/dict naming vs actual DB naming
    # Only flag 'qty' when it appears in SQL strings (inside execute/fetch calls)
    # False positives: JSON keys ("qty":), dict keys ({'qty':), Alpaca attributes (.qty)
    name_mismatches = [
        ("quantity", "qty"),    # DB has quantity, code sometimes uses qty
    ]
    
    for correct, wrong in name_mismatches:
        # Look for 'qty' inside SQL query strings only
        # Pattern: inside a SQL string (single or double quotes) as a column reference
        sql_patterns = re.findall(r"""['"].*?qty.*?['"]""", src, re.DOTALL)
        # Filter out JSON keys, dict keys, and Alpaca attributes
        real_sql_hits = []
        for hit in sql_patterns:
            # Skip if it's a JSON key ("qty": or 'qty':)
            if re.search(r"""['"]qty['"]\s*:""", hit):
                continue
            # Skip if it's a dict key pattern
            if re.search(r"""['"]qty['"]""", hit) and not any(kw in hit.lower() for kw in ["select", "from", "where", "join", "insert", "update", "table"]):
                continue
            real_sql_hits.append(hit)
        
        if real_sql_hits:
            check(f"SQL column '{wrong}' in {filepath.name} — should be '{correct}'",
                  False, f"Found {len(real_sql_hits)} SQL references to 'qty' instead of 'quantity': {[h[:60] for h in real_sql_hits]}")

def test_import_chain():
    """Verify all modules can be imported without errors.
    
    Adds the repo root to sys.path so 'src' package is importable in CI.
    """
    # Mock environment so PG_DSN is set
    os.environ.setdefault("PG_DSN", "host=localhost port=5432 dbname=trading user=trader")
    
    # Add repo root to sys.path for 'src' package imports
    sys.path.insert(0, str(REPO))
    
    modules = [
        ("src.leaderboard_api", ["_get_portfolio", "_get_positions_from_db", "_db", "_PgCursor"]),
    ]
    
    for mod_name, expected_attrs in modules:
        try:
            mod = __import__(mod_name, fromlist=expected_attrs)
            for attr in expected_attrs:
                check(f"Module {mod_name} has attr {attr}", hasattr(mod, attr))
        except Exception as e:
            check(f"Import {mod_name}", False, f"Failed: {e}")

def _has_db():
    """Check if PostgreSQL is reachable."""
    try:
        import psycopg2
        dsn = os.getenv("PG_DSN", "host=trading-db port=5432 dbname=trading user=trader")
        conn = psycopg2.connect(dsn, connect_timeout=3)
        conn.close()
        return True
    except Exception:
        return False

def _has_dashboard():
    """Check if the dashboard API is reachable."""
    import urllib.request
    base = os.environ.get("TEST_API_BASE", "http://localhost:5002")
    try:
        urllib.request.urlopen(f"{base}/api/traders", timeout=3)
        return True
    except Exception:
        return False

def test_api_endpoints():
    """Verify API endpoints return 200 with expected structure.
    
    Skipped when dashboard is not running (Phase 1 CI).
    """
    if not _has_dashboard():
        check("API endpoints", True, "Dashboard not available — skipped")
        return
    
    import urllib.request
    import json as json_lib
    
    endpoints = [
        "/api/traders",
        "/api/summary",
        "/api/trades",
        "/api/decisions",
        "/api/pnl",
    ]
    
    base = os.environ.get("TEST_API_BASE", "http://localhost:5002")
    
    for ep in endpoints:
        try:
            req = urllib.request.Request(f"{base}{ep}")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json_lib.loads(resp.read())
                check(f"API {ep} returns 200", resp.status == 200)
                check(f"API {ep} returns valid JSON", data is not None)
        except Exception as e:
            check(f"API {ep}", False, f"Failed: {e}")

def test_db_connection():
    """Verify PG_DSN connects and schema is intact.
    
    Skipped when PostgreSQL is not reachable (Phase 1 CI).
    """
    if not _has_db():
        check("DB connection", True, "PostgreSQL not available — skipped")
        return
    
    import psycopg2
    import psycopg2.extras
    
    dsn = os.getenv("PG_DSN", "host=trading-db port=5432 dbname=trading user=trader")
    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # Check required tables exist
        cur.execute("""
            SELECT table_name FROM information_schema.tables 
            WHERE table_schema = 'trading'
        """)
        tables = [r['table_name'] for r in cur.fetchall()]
        required = ['portfolio_snapshots', 'trader_positions', 'trades', 'decisions', 'quotes']
        for t in required:
            check(f"Table trading.{t} exists", t in tables)
        
        # Check trader_positions columns
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'trading' AND table_name = 'trader_positions'
        """)
        cols = [r['column_name'] for r in cur.fetchall()]
        expected_cols = ['quantity', 'ticker', 'trader_id', 'status', 'market_value']
        for c in expected_cols:
            check(f"Column trading.trader_positions.{c} exists", c in cols)
        
        # Check portfolio_snapshots has recent data
        cur.execute("""
            SELECT COUNT(*) as cnt FROM trading.portfolio_snapshots
        """)
        count = cur.fetchone()['cnt']
        check(f"Portfolio snapshots has data", count > 0, f"Found {count} rows")
        
        cur.close()
        conn.close()
    except Exception as e:
        check("DB connection", False, f"Failed: {e}")

def test_positions_flow():
    """Verify _get_positions_from_db returns valid data for known traders.
    
    Skipped when PostgreSQL is not reachable (Phase 1 CI).
    """
    if not _has_db():
        check("Positions flow", True, "PostgreSQL not available — skipped")
        return
    
    try:
        sys.path.insert(0, str(REPO / "src"))
        from leaderboard_api import _get_positions_from_db, _get_portfolio, _get_portfolio_from_db
        
        for trader in ["kairos", "aldridge", "stonks"]:
            positions = _get_positions_from_db(trader)
            check(f"Positions for {trader} is list", isinstance(positions, list))
            if positions:
                p = positions[0]
                for field in ["ticker", "quantity", "market_value", "unrealized_pl", "current_price"]:
                    check(f"Position field {field}", field in p)
        
        # Portfolio should not be None
        for trader in ["kairos", "aldridge"]:
            pf = _get_portfolio(trader)
            check(f"Portfolio for {trader}", pf is not None)
            if pf:
                for field in ["portfolio_value", "positions", "_source"]:
                    check(f"Portfolio field {field}", field in pf)
    except Exception as e:
        check("Positions flow", False, f"Failed: {e}")

def test_no_alpaca_dependency():
    """Verify the codebase has no actual Alpaca runtime dependency."""
    import_src = (REPO / "src" / "leaderboard_api.py").read_text()
    
    # _get_alpaca_portfolio should not exist (renamed to _get_portfolio)
    check("No _get_alpaca_portfolio function",
          "_get_alpaca_portfolio" not in import_src)
    
    # AlpacaExecutor should not be imported
    check("No AlpacaExecutor import",
          "AlpacaExecutor" not in import_src)

def run():
    print("=" * 60)
    print("Paper Trading System — Comprehensive Guardrail Tests")
    print("=" * 60)
    
    test_no_alpaca_dependency()
    test_import_chain()
    test_db_connection()
    
    # Check source files for orphaned code and naming issues
    for f in REPO.glob("src/leaderboard_ui/**/*.html"):
        assert_no_orphaned_js(f)
    
    for f in REPO.glob("src/**/*.py"):
        assert_no_silent_except(f)
        assert_column_names_match(f)
    
    test_positions_flow()
    
    if os.getenv("TEST_API") == "1":
        test_api_endpoints()
    
    print(f"\n{'=' * 60}")
    print(f"Results: {len(ERRORS)} failures")
    for e in ERRORS:
        print(e)
    
    return len(ERRORS)

if __name__ == "__main__":
    sys.exit(run())

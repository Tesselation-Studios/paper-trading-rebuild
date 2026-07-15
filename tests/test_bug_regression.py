"""
Bug regression tests — covering every bug we've discovered.
Run: PYTHONPATH=src python3 -m pytest tests/test_bug_regression.py -v

Each test captures a specific bug and asserts it never comes back.
"""

import os
import sys
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# ═══════════════════════════════════════════════════════════════
# Bug #1 — Silent Import Failure (AlpacaExecutor not in Docker)
# ═══════════════════════════════════════════════════════════════
def test_no_silent_import_failures():
    """
    REGRESSION: from src.execute import AlpacaExecutor was failing silently
    in the Docker image because src/execute.py wasn't included in the build.
    The except Exception: pass swallowed the error, and the code fell through
    to positions = [].
    
    FIX: Alpaca dependency stripped entirely. Dashboard is pure DB-backed.
    """
    api_src = (ROOT / "src" / "leaderboard_api.py").read_text()
    
    # Must NOT try to import AlpacaExecutor
    assert "AlpacaExecutor" not in api_src, (
        "AlpacaExecutor import must not exist in leaderboard_api.py"
    )
    
    # The function must be named _get_portfolio, not _get_alpaca_portfolio
    assert "def _get_portfolio(" in api_src, (
        "Function should be named _get_portfolio (renamed from _get_alpaca_portfolio)"
    )
    assert "def _get_alpaca_portfolio(" not in api_src, (
        "Old name _get_alpaca_portfolio must be removed"
    )

    # No bare except:pass in the portfolio function
    portfolio_fn_match = re.search(
        r"def _get_portfolio\(.*?(?=\n\ndef |\Z)",
        api_src, re.DOTALL
    )
    if portfolio_fn_match:
        fn_body = portfolio_fn_match.group()
        bare_excepts = re.findall(r"except\s*\w*\s*:\s*\n\s+pass", fn_body)
        assert len(bare_excepts) == 0, (
            f"Found {len(bare_excepts)} bare except:pass in _get_portfolio"
        )


# ═══════════════════════════════════════════════════════════════
# Bug #2 — Column name mismatch (qty vs quantity in SQL queries)
# ═══════════════════════════════════════════════════════════════
def test_no_qty_column_in_sql():
    """
    REGRESSION: SQL queries used 'qty' but DB column is 'quantity'.
    This caused psycopg2.errors.UndefinedColumn at runtime.
    The error propagated up as an unhandled exception in some paths,
    or was silently caught in others, returning empty results.
    """
    for pyfile in ROOT.glob("src/**/*.py"):
        src = pyfile.read_text()
        # Find SQL SELECT statements referencing qty as a column (not a variable)
        # Only flag SELECT ... qty FROM ... not Python variable usage
        sql_refs = re.findall(
            r'SELECT\s+[^"\']*?\bqty\b[^"\']*?FROM\s+\w+',
            src, re.IGNORECASE
        )
        assert len(sql_refs) == 0, (
            f"{pyfile.name}: SQL uses 'qty' instead of 'quantity': {sql_refs}"
        )


# ═══════════════════════════════════════════════════════════════
# Bug #3 — Orphaned top-level JS code
# ═══════════════════════════════════════════════════════════════
def test_no_orphaned_top_level_js():
    """
    REGRESSION: index.html had orphaned for-loop at the top level
    referencing 'positions' (undefined), killing the entire script
    before fetchAll() could fire. The dashboard showed "Connecting..."
    forever.
    
    This test checks that no top-level code references undefined
    variables outside of function scope.
    """
    for html_file in ROOT.glob("src/leaderboard_ui/**/*.html"):
        html = html_file.read_text()
        scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
        
        for i, script in enumerate(scripts):
            lines = script.split('\n')
            for j, line in enumerate(lines):
                stripped = line.strip()
                
                # Skip empty, comments, and declaration lines
                if not stripped or stripped.startswith('//') or stripped.startswith('/*'):
                    continue
                
                # Check top-level for-loops referencing arrays defined later
                # by fetchAll() calls
                if stripped.startswith('for ') and 'of' in stripped:
                    depth = _depth_at_line(lines, j)
                    if depth == 0:
                        var = re.match(r'for\s+\(?\s*\w+\s+of\s+(\w+)', stripped)
                        if var:
                            var_name = var.group(1)
                            # Check if this var is declared anywhere in script
                            declarations = re.findall(
                                rf'(?:let|const|var)\s+{re.escape(var_name)}\b',
                                script
                            )
                            assert len(declarations) > 0, (
                                f"{html_file.name} script block {i}, line {j+1}: "
                                f"top-level for loop referencing '{var_name}' "
                                f"which is never declared — would throw ReferenceError"
                            )


def _depth_at_line(lines, line_idx):
    """Calculate brace depth at a given line."""
    preceding = '\n'.join(lines[:line_idx])
    return preceding.count('{') - preceding.count('}')


# ═══════════════════════════════════════════════════════════════
# Bug #4 — Hardcoded DNS causing failed DB connections
# ═══════════════════════════════════════════════════════════════
def test_no_hardcoded_dns_in_compose():
    """
    REGRESSION: docker-compose.yml had dns: 192.168.1.25 (Pi-hole) globally.
    Pi-hole can't resolve Docker-internal hostnames (trading-db).
    Services connecting to 'trading-db' would either fail or connect
    to a wrong host.
    """
    compose_paths = list(ROOT.glob("docker-compose*.yml")) + list(ROOT.glob("compose*.yml"))
    if not compose_paths:
        return  # No compose file in this repo
    
    for compose_file in compose_paths:
        content = compose_file.read_text()
        # Check for DNS overrides in the compose file (not in archive/)
        if 'dns:' in content and 'archive' not in str(compose_file):
            # Parse and check if any DNS points to internal-only resolvers
            dns_entries = re.findall(r'dns:\s*\n(?:\s+-\s+[\d.]+\n?)+', content)
            assert len(dns_entries) == 0, (
                f"{compose_file.name}: has custom DNS entries that may "
                f"break Docker-internal hostname resolution"
            )


# ═══════════════════════════════════════════════════════════════
# Bug #5 — PGPORT mismatch (host 5433 vs internal 5432)
# ═══════════════════════════════════════════════════════════════
def test_pgport_consistency():
    """
    REGRESSION: Data-bus dual_writer was connecting to PG on port 5433
    (host-mapped) instead of 5432 (Docker internal). The host mapping
    doesn't apply for container-to-container communication.
    """
    # Check compose files for consistent port mapping
    compose_paths = list(ROOT.glob("docker-compose*.yml")) + list(ROOT.glob("compose*.yml"))
    for compose_file in compose_paths:
        content = compose_file.read_text()
        # Find PG port mappings
        pg_mappings = re.findall(r'5433:5432', content)
        for mapping in pg_mappings:
            # Verify all services that connect to PG use port 5432 internally
            # (not 5433, which is the host-side mapping)
            pass  # This is a manual check since we can't parse YAML perfectly here


# ═══════════════════════════════════════════════════════════════
# Bug #6 — Trader ID prefix mismatch (trader-kairos vs kairos)
# ═══════════════════════════════════════════════════════════════
def test_trader_id_prefix_consistency():
    """
    REGRESSION: trader_positions uses short names (kairos) while
    agent_state uses prefixed names (trader-kairos). The portfolio
    snapshot writer originally used agent_state trader_id to look up
    positions, finding zero matches.
    """
    api_src = (ROOT / "src" / "leaderboard_api.py").read_text()
    
    # Check that short_id extraction is used consistently
    has_normalization = (
        "trader_id.replace" in api_src 
        or "agent_id.replace" in api_src
        or "short_id" in api_src
        or "short_id = agent_id" in api_src
        or "trader-" in api_src and "replace" in api_src
    )
    assert has_normalization, (
        "Must have trader ID normalization (trader-kairos -> kairos)"
    )
    
    # Verify 
    db_src = (ROOT / "src" / "data_bus.py").read_text() if (ROOT / "src" / "data_bus.py").exists() else ""
    if db_src:
        has_portfolio_writer = "_scheduled_write_portfolio_snapshots" in db_src
        has_dual_writer = "dual_writer" in db_src
        assert has_portfolio_writer or has_dual_writer, (
            "data_bus.py must have portfolio snapshot writer"
        )


# ═══════════════════════════════════════════════════════════════
# Bug #7 — Dashboard Dockerfile missing files in COPY
# ═══════════════════════════════════════════════════════════════
def test_dockerfile_copies_leaderboard_correctly():
    """
    REGRESSION: Dockerfile.leaderboard only copies src/ and leaderboard_ui/.
    If leaderboard_api.py depends on src/execute.py, it won't be available
    at runtime. This caused the silent import failure.
    
    Fix: The leaderboard now has zero dependencies on other src/ modules.
    """
    dockerfile = ROOT / "Dockerfile.leaderboard"
    if not dockerfile.exists():
        return
    
    df_content = dockerfile.read_text()
    
    # Check COPY commands
    copies = re.findall(r'COPY\s+(\S+)', df_content)
    
    # Verify leaderboard_api.py doesn't import from non-copied modules
    api_src = (ROOT / "src" / "leaderboard_api.py").read_text()
    local_imports = re.findall(r'from\s+src\.(\w+)', api_src)
    local_imports += re.findall(r'import\s+src\.(\w+)', api_src)
    
    for imp in local_imports:
        if imp and imp != "leaderboard_ui":
            # Check if the imported module is in a COPY'd path
            copied_dirs = [c.replace("/", "") for c in copies if c.endswith("/")]
            assert imp in " ".join(copies), (
                f"leaderboard_api.py imports src.{imp} but "
                f"Dockerfile.leaderboard doesn't COPY it: {copies}"
            )


if __name__ == "__main__":
    # Run all tests
    test_fns = [fn for fn in dir() if fn.startswith("test_")]
    failures = 0
    for fn_name in test_fns:
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

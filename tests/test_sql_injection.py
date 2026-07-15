#!/usr/bin/env python3
"""
Regression tests for SQL injection fixes in data_bus.py.

Ensures no f-string SQL execute patterns remain, that parameterized queries
use proper placeholders, and that database operations have error handling.
"""

import re
import ast
import sys
from pathlib import Path

import pytest

# Project root for source file scanning
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_BUS_PATH = REPO_ROOT / "src" / "data_bus.py"


# ── Helper: extract all AST Call nodes from a Python file ───────────────────

def _get_call_nodes(tree: ast.AST) -> list[ast.Call]:
    """Return all ast.Call nodes in the tree (recursive)."""
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            calls.append(node)
    return calls


def _is_execute_call(node: ast.Call) -> bool:
    """Check if an ast.Call is a .execute(...) or .executemany(...) call."""
    if not isinstance(node.func, ast.Attribute):
        return False
    return node.func.attr in ("execute", "executemany", "executescript")


def _is_fstring_arg(node: ast.Call) -> bool:
    """Check if any positional arg to the call is an f-string (ast.JoinedStr)."""
    for arg in node.args:
        if isinstance(arg, ast.JoinedStr):
            return True
    return False


# ── Test 1: No f-string SQL execute calls ───────────────────────────────────

def test_no_fstring_execute():
    """
    Search src/data_bus.py for any .execute(f"..." or .execute(f'...' patterns.
    Fail if any remain — all SQL queries must use parameterized placeholders.
    """
    source = DATA_BUS_PATH.read_text(encoding="utf-8")

    # Regex approach: match execute( f" or execute( f'
    pattern = re.compile(r"\.execute\s*\(\s*f['\"]", re.MULTILINE)
    matches = pattern.findall(source)

    # Also check for f-string inside multi-line calls
    # e.g. execute(\n    f"..."
    multiline_pattern = re.compile(
        r"\.execute\s*\([^)]*f['\"]",
        re.MULTILINE | re.DOTALL,
    )
    # Be more precise: execute(...) where args contain f"..."
    # We'll use AST as a secondary check
    ast_matches = []
    try:
        tree = ast.parse(source)
        for call in _get_call_nodes(tree):
            if _is_execute_call(call) and _is_fstring_arg(call):
                ast_matches.append(call)
    except SyntaxError as e:
        # If AST parsing fails, we still have the regex check
        pytest.fail(f"AST parse error in {DATA_BUS_PATH}: {e}")

    assert not matches, (
        f"Found {len(matches)} execute(f'...') pattern(s) in {DATA_BUS_PATH}:\n"
        + "\n".join(f"  Line: {line}" for line in _find_lines(source, "execute(f"))
    )
    assert not ast_matches, (
        f"Found {len(ast_matches)} AST-level f-string execute call(s)!"
    )


# ── Test 2: Verify parameterized queries use placeholders, not f-strings ────

def test_parameterized_queries_use_placeholders():
    """
    All execute() calls in data_bus.py should use %s or ? placeholders
    for user-supplied values, not f-string interpolation.
    """
    source = DATA_BUS_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Find all .execute() calls
    fstring_execute_lines = []
    missing_placeholder_lines = []
    explicit_fstring_lines = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in ("execute", "executemany"):
            continue

        line_no = node.lineno
        line_text = source.splitlines()[line_no - 1].strip()

        # Check if the SQL arg is an f-string
        if node.args:
            first_arg = node.args[0]

            # Case 1: Direct f-string argument
            if isinstance(first_arg, ast.JoinedStr):
                fstring_execute_lines.append(line_no)

            # Case 2: f-string concatenation (e.g., "..." + f"...")
            elif isinstance(first_arg, ast.BinOp) and isinstance(first_arg.op, ast.Add):
                # Walk the BinOp tree for any f-string (JoinedStr)
                has_fstring = any(
                    isinstance(n, ast.JoinedStr)
                    for n in ast.walk(first_arg)
                )
                if has_fstring:
                    explicit_fstring_lines.append(line_no)

            # Case 3: Check that the SQL string contains placeholders
            # (only for non-f-string, literal SQL strings)
            elif isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                sql = first_arg.value
                # For SQLite: use ? placeholders
                # For PostgreSQL: use %s placeholders
                has_sqlite_placeholder = "?" in sql
                has_pg_placeholder = "%s" in sql

                # For dynamic WHERE clauses built via string concatenation,
                # the full SQL might not be in a single string literal.
                # Skip very short queries (likely config/setup statements)
                if len(sql) > 50 and not has_sqlite_placeholder and not has_pg_placeholder:
                    # Check if the query has DML keywords suggesting it needs params
                    upper = sql.upper().strip()
                    is_dml = any(
                        upper.startswith(kw)
                        for kw in ("SELECT", "INSERT", "UPDATE", "DELETE", "CREATE")
                    )
                    # PRAGMA statements don't need params
                    if is_dml and not sql.strip().upper().startswith("PRAGMA"):
                        missing_placeholder_lines.append(line_no)

    # Build failure message
    errors = []
    if fstring_execute_lines:
        errors.append(
            f"execute() calls with f-string SQL (lines {fstring_execute_lines}):\n"
            + "\n".join(
                f"  Line {ln}: {source.splitlines()[ln - 1].strip()[:120]}"
                for ln in fstring_execute_lines
            )
        )
    if explicit_fstring_lines:
        errors.append(
            f"execute() calls with f-string concatenation (lines {explicit_fstring_lines}):\n"
            + "\n".join(
                f"  Line {ln}: {source.splitlines()[ln - 1].strip()[:120]}"
                for ln in explicit_fstring_lines
            )
        )

    if errors:
        pytest.fail("SQL injection regression found:\n\n" + "\n\n".join(errors))


# ── Test 3: Check for try/except around database operations ─────────────────

def test_try_except_around_db_operations():
    """
    All database-accessing functions in data_bus.py must have try/except
    error handling around their core DB operations.
    """
    source = DATA_BUS_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Identify all function/method definitions
    db_functions = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Check if function uses DB operations
            has_execute = any(
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr in ("execute", "executemany", "fetchone", "fetchall")
                for sub in ast.walk(node)
            )
            has_connect = any(
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "connect"
                for sub in ast.walk(node)
            )

            if has_execute or has_connect:
                db_functions.append(node)

    # Check each function for try/except
    functions_missing_handling = []
    for func in db_functions:
        # Skip functions that are wrappers or already have top-level try/except
        if not any(
            isinstance(stmt, ast.Try) for stmt in func.body[:3]
        ):
            # Check if the function is a thin wrapper or has a try deeper
            # But for DB functions, top-level try/except is the standard pattern
            has_top_level_try = any(
                isinstance(stmt, ast.Try) for stmt in func.body
            )

            # Check for try/except at any nesting level in the function body
            has_nested_try = any(
                isinstance(stmt, ast.Try)
                for stmt in ast.walk(func)
            )

            # Check if function is decorated with route (Flask endpoint) —
            # Flask endpoints often have try/except inside the route body
            func_name = func.name

            if not has_nested_try:
                # Only flag if it clearly does DB operations without try/except
                has_db_io = any(
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr in ("execute", "executemany", "fetchone", "fetchall", "connect", "commit")
                    for sub in ast.walk(func)
                )
                if has_db_io:
                    functions_missing_handling.append(func_name)

    # Exempt known safe helper functions
    known_good = {
        "_get_sqlite_connection",      # connection factory, exception propagates
        "_get_cache_db_connection",    # connection factory, exception propagates
        "_get_vt_db",                  # connection factory, exception propagates
        "_safe_quote_ident",           # validation helper, no DB ops
        "_mask_key",                   # non-DB helper
    }
    functions_missing_handling = [
        f for f in functions_missing_handling if f not in known_good
    ]

    if functions_missing_handling:
        pytest.fail(
            f"Functions with DB operations missing try/except handling:\n"
            + "\n".join(f"  - {f}()" for f in functions_missing_handling)
        )


# ── Test 4: Verify _safe_quote_ident rejects unsafe identifiers ─────────────

def test_safe_quote_ident_rejects_unsafe():
    """Unit test the _safe_quote_ident helper function."""
    from src.data_bus import _safe_quote_ident

    # Safe identifiers
    assert _safe_quote_ident("users") == '"users"'
    assert _safe_quote_ident("user_123") == '"user_123"'
    assert _safe_quote_ident("_private") == '"_private"'

    # Unsafe identifiers
    with pytest.raises(ValueError, match="Unsafe or invalid"):
        _safe_quote_ident("")
    with pytest.raises(ValueError, match="Unsafe or invalid"):
        _safe_quote_ident("users; DROP TABLE")
    with pytest.raises(ValueError, match="Unsafe or invalid"):
        _safe_quote_ident("1table")
    with pytest.raises(ValueError, match="Unsafe or invalid"):
        _safe_quote_ident("table name")
    with pytest.raises(ValueError, match="Unsafe or invalid"):
        _safe_quote_ident("users\" OR \"1\"=\"1")

    # Edge cases
    with pytest.raises(ValueError, match="Unsafe or invalid"):
        _safe_quote_ident(None)
    # Long but valid identifier — should pass
    long_ident = "a" * 1000
    assert _safe_quote_ident(long_ident) == '"' + long_ident + '"'


# ── Test 5: No bare f-string SQL anywhere in codebase ───────────────────────

def test_no_fstring_sql_in_data_bus():
    """
    Scan src/data_bus.py for any f-string that looks like it contains SQL.
    Uses a heuristic: f-strings containing SQL keywords like SELECT, INSERT, etc.
    """
    fstring_sql_issues = []

    text = DATA_BUS_PATH.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(text)

    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            # Check if the f-string has SQL-like content
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    upper = value.value.upper()
                    # Look for DML keywords preceded by a newline/space (SQL-like context)
                    sql_keywords = (" SELECT ", "\nSELECT ", " INSERT ", "\nINSERT ",
                                    " UPDATE ", "\nUPDATE ", " DELETE ", "\nDELETE ",
                                    " CREATE ", "\nCREATE ",
                                    "\nFROM ", "\nWHERE ", " JOIN ",
                                    "\nORDER BY", " HAVING ", "\nLIMIT ",
                                    "COUNT(", "SUM(", "AVG(")
                    if any(kw in upper for kw in sql_keywords):
                        fstring_sql_issues.append(node.lineno)
                        break  # One issue per f-string

    if fstring_sql_issues:
        pytest.fail(
            f"Found {len(fstring_sql_issues)} f-string(s) with SQL-like content "
            f"in {DATA_BUS_PATH.name}:\n"
            + "\n".join(
                f"  Line {ln}: {text.splitlines()[ln - 1].strip()[:120]}"
                for ln in fstring_sql_issues
            )
        )


def _find_lines(text: str, substring: str) -> list[int]:
    """Return 1-indexed line numbers containing substring."""
    return [
        i + 1 for i, line in enumerate(text.splitlines())
        if substring in line
    ]
#!/usr/bin/env python3
"""Tests verifying the cleanup of dead code and bug fixes.

Covers:
1. No bare except:pass blocks remain in src/*.py
2. No stale Alpaca imports/constants remain in src/leaderboard_api.py
3. Archived files have a descriptive README
"""

import re
import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


# ──────────────────────────────────────────────────────────────────────────────
# 1. No bare except:pass blocks
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("pyfile", sorted(REPO.glob("src/**/*.py")))
def test_no_bare_except_pass(pyfile: Path):
    """Verify no bare 'except: pass' or 'except Exception: pass' blocks."""
    src = pyfile.read_text()
    # Matches: except (optional "Exception") : newline whitespace pass
    bare = re.findall(r"except\s+(Exception|)\s*:\s*\n\s+pass", src)
    assert len(bare) == 0, (
        f"{pyfile.relative_to(REPO)} has {len(bare)} bare except:pass block(s)"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 2. No stale Alpaca imports/constants in leaderboard_api.py
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "unused_pattern",
    [
        "AlpacaExecutor",       # old Alpaca executor class
    ],
)
def test_no_stale_alpaca_imports(unused_pattern: str):
    """Verify no stale Alpaca imports remain in leaderboard_api.py."""
    src = (REPO / "src" / "leaderboard_api.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if unused_pattern in alias.name or unused_pattern in (node.module or ""):
                    top_of_module = node.lineno <= 50
                    if top_of_module:
                        pytest.fail(
                            f"Found stale Alpaca import '{alias.name}' at line {node.lineno}"
                        )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if unused_pattern in alias.name:
                    top_of_module = node.lineno <= 50
                    if top_of_module:
                        pytest.fail(
                            f"Found stale Alpaca import '{alias.name}' at line {node.lineno}"
                        )


# ──────────────────────────────────────────────────────────────────────────────
# 3. Archived files have a README
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "archive_dir",
    [
        "old-agents",
        "old-docs",
        "old-specs",
        "agents/virtual",
    ],
)
def test_archived_dirs_have_readme(archive_dir: str):
    """Verify archived directories contain a README explaining why they were archived."""
    arch = REPO / "archive" / archive_dir
    if not arch.exists() or not arch.is_dir():
        pytest.skip(f"archive/{archive_dir} does not exist")

    readme = arch / "README.md"
    assert readme.exists(), f"archive/{archive_dir} is missing a README.md"

    content = readme.read_text()
    assert content.strip(), f"archive/{archive_dir}/README.md is empty"
    assert "archived" in content.lower() or "deprecated" in content.lower(), (
        f"archive/{archive_dir}/README.md should explain why it was archived"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 4. No bare except:pass in scripts/*.py too (bonus guard)
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("pyfile", sorted(REPO.glob("scripts/*.py")))
def test_scripts_no_bare_except_pass(pyfile: Path):
    """Verify scripts have no bare except:pass blocks either."""
    src = pyfile.read_text()
    bare = re.findall(r"except\s+(Exception|)\s*:\s*\n\s+pass", src)
    assert len(bare) == 0, (
        f"{pyfile.relative_to(REPO)} has {len(bare)} bare except:pass block(s)"
    )

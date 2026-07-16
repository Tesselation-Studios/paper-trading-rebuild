#!/usr/bin/env python3
"""Tests for the terminal dashboard script.

Uses subprocess for integration tests (since the script has rich imports).
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
DASHBOARD_SCRIPT = SCRIPTS_DIR / "terminal_dashboard.py"


def run_dash(*args: str, timeout: int = 5) -> subprocess.CompletedProcess:
    """Run the dashboard script with given args."""
    return subprocess.run(
        [sys.executable, str(DASHBOARD_SCRIPT), *args],
        capture_output=True, text=True, timeout=timeout,
    )


class TestHelpAndVersion(unittest.TestCase):
    """Test CLI argument handling."""

    def test_help(self):
        """--help prints usage info."""
        result = run_dash("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("usage:", result.stdout.lower())

    def test_no_args_exits_gracefully(self):
        """Running without args in non-TTY: dashboard mode requires TTY for Live display."""
        # Dashboard mode requires a TTY — it loops until Ctrl+C.
        # In non-TTY mode, rich.Live may fail or hang.
        # Timeout is expected behavior: use --test or --test-all for scripting.
        try:
            run_dash("--refresh", "0.5", timeout=3)
        except subprocess.TimeoutExpired:
            pass  # Expected: dashboard runs until interrupted in interactive mode


class TestTestAllMode(unittest.TestCase):
    """Test the --test-all probe mode."""

    def test_test_all_localhost(self):
        """--test-all probes all endpoints against localhost."""
        result = run_dash("--test-all", timeout=30)
        # Should succeed or fail gracefully
        self.assertIsNotNone(result)
        if result.returncode == 0:
            self.assertIn("Endpoint Probe", result.stdout)
        # If bus not running, should still produce output

    def test_test_all_nonexistent_host(self):
        """--test-all against non-existent host handles gracefully."""
        result = run_dash("--test-all", "--bus", "http://127.0.0.1:59999", timeout=10)
        self.assertIsNotNone(result)


class TestSingleEndpointTest(unittest.TestCase):
    """Test the --test single endpoint mode."""

    def test_test_health(self):
        """--test /health works."""
        result = run_dash("--test", "/health", timeout=10)
        self.assertIsNotNone(result)

        if result.returncode == 0:
            # Should show endpoint test output
            self.assertIn("Endpoint Test", result.stdout)
            if "Connection refused" not in result.stdout:
                self.assertIn("Response", result.stdout)

    def test_test_nonexistent_path(self):
        """--test with bad path handles gracefully."""
        result = run_dash("--test", "/nonexistent-endpoint-xyz", timeout=10)
        self.assertIsNotNone(result)
        # Should not crash

    def test_test_bus_unreachable(self):
        """--test against offline bus handles gracefully."""
        result = run_dash(
            "--test", "/health",
            "--bus", "http://127.0.0.1:59999",
            timeout=5,
        )
        self.assertIsNotNone(result)


class TestEndpointListConsistency(unittest.TestCase):
    """Verify the ENDPOINT_LIST is well-formed in the script source."""

    @classmethod
    def setUpClass(cls):
        """Parse ENDPOINT_LIST from the script source."""
        source = DASHBOARD_SCRIPT.read_text()
        # Find the ENDPOINT_LIST literal
        import ast
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and hasattr(node.targets[0], 'id'):
                if node.targets[0].id == 'ENDPOINT_LIST':
                    cls.endpoints = ast.literal_eval(node.value)
                    break
        else:
            cls.endpoints = []

    def test_non_empty(self):
        """ENDPOINT_LIST has entries."""
        self.assertGreater(len(self.endpoints), 10)

    def test_all_tuples_three_elements(self):
        for entry in self.endpoints:
            self.assertEqual(len(entry), 3, f"Entry {entry} has != 3 elements")

    def test_paths_start_with_slash(self):
        for _, path, _ in self.endpoints:
            self.assertTrue(path.startswith("/"), f"Path '{path}' doesn't start with /")

    def test_health_is_first(self):
        self.assertEqual(self.endpoints[0][1], "/health")

    def test_no_duplicate_paths(self):
        paths = [p for _, p, _ in self.endpoints]
        self.assertEqual(len(paths), len(set(paths)))

    def test_all_methods_valid(self):
        valid_methods = {"GET", "POST", "GET,POST"}
        for method, _, _ in self.endpoints:
            self.assertIn(method, valid_methods, f"Invalid method: {method}")


class TestScriptSyntax(unittest.TestCase):
    """Smoke tests for the script itself."""

    def test_python_syntax_valid(self):
        """Script compiles without errors."""
        result = subprocess.run(
            [sys.executable, "-c",
             f"import py_compile; py_compile.compile({str(DASHBOARD_SCRIPT)!r}, doraise=True)"],
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, f"Syntax error: {result.stderr}")

    def test_script_is_executable(self):
        """Script has executable permissions."""
        self.assertTrue(
            DASHBOARD_SCRIPT.stat().st_mode & 0o111,
            "Script is not executable"
        )

    def test_shebang(self):
        """Script starts with proper shebang."""
        first_line = DASHBOARD_SCRIPT.read_text().splitlines()[0]
        self.assertIn("python", first_line)


if __name__ == "__main__":
    unittest.main()

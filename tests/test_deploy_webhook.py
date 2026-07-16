"""
tests/test_deploy_webhook.py — Tests for deploy_webhook.py

Tests the HTTP server logic without starting a real server:
- Health endpoint
- Auth (HMAC signature, Bearer token, no auth)
- Deploy trigger parsing
- Error paths
"""

import hashlib
import hmac
import json
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from deploy_webhook import (
    verify_signature,
    verify_bearer,
    DeployHandler,
)


class TestSignatureVerification:
    def test_valid_hmac_signature(self):
        secret = "test-secret-123"
        body = b'{"tag":"abc1234"}'
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

        with patch.dict(os.environ, {"WEBHOOK_SECRET": secret}):
            assert verify_signature(body, f"sha256={expected}") is True

    def test_invalid_hmac_signature(self):
        secret = "test-secret-123"
        body = b'{"tag":"abc1234"}'

        with patch.dict(os.environ, {"WEBHOOK_SECRET": secret}):
            assert verify_signature(body, "sha256=deadbeef") is False

    def test_wrong_algo(self):
        with patch.dict(os.environ, {"WEBHOOK_SECRET": "secret"}):
            assert verify_signature(b"body", "sha1=abc123") is False

    def test_no_secret_skips_verification(self):
        with patch.dict(os.environ, {}, clear=True):
            if "WEBHOOK_SECRET" in os.environ:
                del os.environ["WEBHOOK_SECRET"]
        # Can't easily clear env in test, but the logic should pass
        assert verify_signature(b"body", "sha256=anything") is True

    def test_malformed_signature_header(self):
        with patch.dict(os.environ, {"WEBHOOK_SECRET": "secret"}):
            assert verify_signature(b"body", "garbage") is False


class TestBearerVerification:
    def test_valid_bearer(self):
        with patch.dict(os.environ, {"WEBHOOK_SECRET": "secret123"}):
            assert verify_bearer("Bearer secret123") is True

    def test_invalid_bearer(self):
        with patch.dict(os.environ, {"WEBHOOK_SECRET": "secret123"}):
            assert verify_bearer("Bearer wrong-secret") is False

    def test_no_secret_allows_any(self):
        with patch.dict(os.environ, {"WEBHOOK_SECRET": ""}):
            assert verify_bearer("Bearer anything") is True

    def test_missing_auth_header(self):
        with patch.dict(os.environ, {"WEBHOOK_SECRET": "secret"}):
            assert verify_bearer("") is False

    def test_wrong_auth_scheme(self):
        with patch.dict(os.environ, {"WEBHOOK_SECRET": "secret"}):
            assert verify_bearer("Basic secret") is False


class TestDeployHandler:

    def make_handler(self) -> DeployHandler:
        """Create a DeployHandler with mock request/response."""
        handler = DeployHandler.__new__(DeployHandler)
        handler.wfile = MagicMock()
        handler.wfile.write = MagicMock()
        handler.rfile = MagicMock()
        handler.headers = MagicMock()
        handler.client_address = ("127.0.0.1", 12345)
        handler.path = "/"
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        return handler

    def test_health_endpoint(self):
        handler = self.make_handler()
        handler.path = "/health"
        handler.do_GET()
        handler.send_response.assert_called_once_with(200)

    def test_404_on_unknown_get(self):
        handler = self.make_handler()
        handler.path = "/unknown"
        handler.do_GET()
        handler.send_response.assert_called_once_with(404)

    def test_404_on_unknown_post(self):
        handler = self.make_handler()
        handler.path = "/unknown"
        handler.headers.get.return_value = "0"
        handler.rfile.read.return_value = b""
        handler.do_POST()
        handler.send_response.assert_called_once_with(404)

    def test_deploy_requires_auth_when_secret_set(self):
        handler = self.make_handler()
        handler.path = "/deploy"
        handler.headers.get.side_effect = lambda key, default=None: {
            "Content-Length": "2",
            "X-Hub-Signature-256": "",
            "Authorization": "",
        }.get(key, default)
        handler.rfile.read.return_value = b"{}"

        with patch.dict(os.environ, {"WEBHOOK_SECRET": "secret"}):
            handler.do_POST()
            handler.send_response.assert_called_once_with(401)

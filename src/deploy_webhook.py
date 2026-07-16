#!/usr/bin/env python3
"""
deploy_webhook.py — HTTP webhook receiver for automated Docker deploys.

Receives GitHub-style push events (or minimal deploy triggers) and
executes scripts/deploy.sh with the specified tag.

Usage:
    python3 src/deploy_webhook.py --port 5099 --secret WEBHOOK_SECRET

Security:
    - HMAC-SHA256 verification if X-Hub-Signature-256 header is present
    - Falls back to shared-secret Bearer token check
    - Only accepts POST to /deploy

docker-compose snippet:
    deploy-webhook:
        build: .
        container_name: trading-deploy-webhook
        restart: unless-stopped
        ports:
          - "5099:5099"
        environment:
          - WEBHOOK_SECRET=${WEBHOOK_SECRET}
          - GHCR_USER=${GHCR_USER}
          - GHCR_TOKEN=${GHCR_TOKEN}
        volumes:
          - /var/run/docker.sock:/var/run/docker.sock
        working_dir: /app
        command: python3 src/deploy_webhook.py --port 5099
"""

import argparse
import hashlib
import hmac
import json
import logging
import os
import subprocess
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [deploy-webhook] %(message)s",
)
log = logging.getLogger("deploy-webhook")

DEPLOY_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "deploy.sh")


def _get_secret() -> str:
    return os.environ.get("WEBHOOK_SECRET", "")


def verify_signature(body: bytes, signature_header: str) -> bool:
    """Verify HMAC-SHA256 signature from GitHub webhook."""
    secret = _get_secret()
    if not secret:
        log.warning("WEBHOOK_SECRET not set — skipping signature verification")
        return True
    try:
        algo, sig = signature_header.split("=", 1)
        if algo != "sha256":
            return False
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)
    except Exception:
        return False


def verify_bearer(auth_header: str) -> bool:
    """Verify Bearer token matches WEBHOOK_SECRET."""
    secret = _get_secret()
    if not secret:
        return True
    if not auth_header or not auth_header.startswith("Bearer "):
        return False
    token = auth_header[7:]
    return hmac.compare_digest(token.encode(), secret.encode())


def run_deploy(tag: str) -> tuple[bool, str]:
    """Execute deploy.sh with the given tag."""
    try:
        result = subprocess.run(
            ["bash", DEPLOY_SCRIPT, tag],
            capture_output=True,
            text=True,
            timeout=300,
            env={**os.environ, "GHCR_USER": os.environ.get("GHCR_USER", ""),
                 "GHCR_TOKEN": os.environ.get("GHCR_TOKEN", "")},
        )
        output = result.stdout + result.stderr
        success = result.returncode == 0
        return success, output
    except subprocess.TimeoutExpired:
        return False, "Deploy script timed out after 300s"
    except Exception as e:
        return False, str(e)


class DeployHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info(f"{self.client_address[0]} — {fmt % args}")

    def _respond(self, code: int, body: dict):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_GET(self):
        if self.path == "/health":
            self._respond(200, {"status": "ok", "service": "deploy-webhook"})
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/deploy":
            self._respond(404, {"error": "not found"})
            return

        # Read body
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        # Auth: prefer HMAC signature, fall back to Bearer token
        signature = self.headers.get("X-Hub-Signature-256", "")
        auth = self.headers.get("Authorization", "")

        if signature:
            if not verify_signature(body, signature):
                self._respond(403, {"error": "invalid signature"})
                return
        elif auth:
            if not verify_bearer(auth):
                self._respond(403, {"error": "invalid token"})
                return
        else:
            if _get_secret():
                self._respond(401, {"error": "authentication required"})
                return

        # Parse payload
        tag = "latest"
        try:
            payload = json.loads(body) if body else {}
            # GitHub push event: extract commit SHA
            if "after" in payload and payload.get("ref") == "refs/heads/main":
                tag = payload["after"][:7]
            elif "tag" in payload:
                tag = payload["tag"]
        except json.JSONDecodeError:
            pass

        log.info(f"Deploy triggered — tag: {tag}")
        success, output = run_deploy(tag)

        if success:
            self._respond(200, {"status": "deployed", "tag": tag, "output": output[-2000:]})
        else:
            self._respond(500, {"status": "failed", "tag": tag, "output": output[-2000:]})


def main():
    parser = argparse.ArgumentParser(description="Deploy webhook receiver")
    parser.add_argument("--port", type=int, default=5099)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    if not os.path.exists(DEPLOY_SCRIPT):
        log.error(f"Deploy script not found: {DEPLOY_SCRIPT}")
        sys.exit(1)

    server = HTTPServer((args.host, args.port), DeployHandler)
    log.info(f"Deploy webhook listening on {args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        server.server_close()


if __name__ == "__main__":
    main()

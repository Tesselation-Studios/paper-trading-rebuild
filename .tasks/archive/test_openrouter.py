#!/usr/bin/env python3
"""Test OpenRouter key validity."""
import os
import urllib.request
import json

key = os.getenv("OPENROUTER_API_KEY", "")
print(f"Key length: {len(key)}, starts with: {key[:6]}...")

req = urllib.request.Request(
    "https://openrouter.ai/api/v1/models",
    headers={"Authorization": f"Bearer {key}"}
)
try:
    resp = urllib.request.urlopen(req, timeout=10)
    data = json.loads(resp.read())
    models = [m["id"] for m in data.get("data", [])][:10]
    print(f"Key valid! Available models: {models}")
except urllib.error.HTTPError as e:
    body = e.read().decode()[:300]
    print(f"HTTP {e.code}: {body}")
except Exception as e:
    print(f"Error: {e}")
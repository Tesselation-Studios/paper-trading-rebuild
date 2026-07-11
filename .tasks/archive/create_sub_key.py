#!/usr/bin/env python3
"""Create an OpenRouter sub-key from the provisioning key."""
import os
import urllib.request
import json

admin_key = os.getenv("OPENROUTER_API_KEY", "")
print(f"Admin key length: {len(admin_key)}")

data = json.dumps({
    "name": "trading-virtual-runner",
    "limit": 20,
}).encode()
req = urllib.request.Request(
    "https://openrouter.ai/api/v1/keys",
    data=data,
    headers={
        "Authorization": f"Bearer {admin_key}",
        "Content-Type": "application/json"
    }
)
resp = urllib.request.urlopen(req, timeout=15)
result = json.loads(resp.read())

# The full key is in result["key"]
full_key = result.get("key", "")
print(f"FULL_KEY={full_key}")
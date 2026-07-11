#!/usr/bin/env python3
"""Create OpenRouter sub-key from provisioning key and print the full key."""
import os
import sys
import urllib.request
import json

env_path = '/home/raf/trading/.env'
admin_key = ''
with open(env_path) as f:
    for line in f:
        line = line.strip()
        if line.startswith('OPENROUTER_API_KEY='):
            admin_key = line.split('=', 1)[1].strip().strip('"').strip("'")
            break

if not admin_key:
    print('ERROR: No OPENROUTER_API_KEY found in .env')
    sys.exit(1)

print(f'Admin key found: {admin_key[:8]}...{admin_key[-4:]}')

# Create sub-key
data = json.dumps({'name': 'trading-vr', 'limit': 20}).encode()
req = urllib.request.Request(
    'https://openrouter.ai/api/v1/keys',
    data=data,
    headers={
        'Authorization': f'Bearer {admin_key}',
        'Content-Type': 'application/json'
    }
)
try:
    resp = urllib.request.urlopen(req, timeout=15)
    result = json.loads(resp.read())
    full_key = result.get('key', '')
    if full_key:
        print(f'NEW_KEY={full_key}')
    else:
        print('ERROR: No key in response')
        print(json.dumps(result, indent=2))
except urllib.error.HTTPError as e:
    body = e.read().decode()[:500]
    print(f'HTTP {e.code}: {body}')
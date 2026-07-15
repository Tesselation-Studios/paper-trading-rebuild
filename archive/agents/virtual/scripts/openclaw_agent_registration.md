# Virtual Competitor OpenClaw Agent Registration

Add these agent entries to `~/.openclaw/openclaw.json` under `agents.list`.

Each virtual competitor has:
- A dedicated workspace under `/home/openclaw/.openclaw/workspace-virtual-{base}-{type}`
- A 5-minute heartbeat during market hours
- A skill that loads the virtual variant's AGENTS.md/config.yaml
- Same model tier as the live trader (deepseek-v4-flash)

## Instructions

After adding these to openclaw.json, run:
  `openclaw gateway restart` (explicit user request)

Then create each workspace directory:
  `mkdir -p /home/openclaw/.openclaw/workspace-virtual-{base}-{type}/{skills,memory}`

Copy the variant files:
  `cp agents/virtual/{base}/{type}/*.md /home/openclaw/.openclaw/workspace-virtual-{base}-{type}/`
  `cp agents/virtual/{base}/{type}/skills/SKILL.md /home/openclaw/.openclaw/workspace-virtual-{base}-{type}/skills/`

Copy the heartbeat template:
  `cp agents/virtual/scripts/HEARTBEAT.md.template /home/openclaw/.openclaw/workspace-virtual-{base}-{type}/HEARTBEAT.md`
  (Edit the variant-specific path)

## Agent Config Blocks

Paste these blocks into openclaw.json `agents.list`. One per virtual variant.

### Kairos Variants

```json
{
    "id": "virtual-kairos-aggressive",
    "name": "Virtual Kairos — Aggressive",
    "workspace": "/home/openclaw/.openclaw/workspace-virtual-kairos-aggressive",
    "skills": ["trade-execution", "market-data", "risk-management", "trading-hours", "virtual-competitor"],
    "model": {
        "primary": "openrouter/deepseek/deepseek-v4-flash",
        "fallbacks": [
            "openrouter/deepseek/deepseek-v4-pro",
            "openrouter/deepseek/deepseek-v3.2",
            "openrouter/minimax/minimax-m3",
            "openrouter/qwen/qwen3.7-plus",
            "anthropic/claude-sonnet-4-6",
            "openrouter/auto"
        ]
    },
    "heartbeat": {
        "every": "5m",
        "timeoutSeconds": 900,
        "lightContext": true,
        "prompt": "Read /home/openclaw/.openclaw/workspace-virtual-kairos-aggressive/AGENTS.md\nRead HEARTBEAT.md. Follow the core flow.\nThis is a VIRTUAL COMPETITOR variant. Make independent trading decisions.\nOutput HEARTBEAT_OK when done.",
        "skipWhenBusy": false,
        "activeHours": {
            "start": "09:30",
            "end": "16:00",
            "timezone": "America/New_York"
        }
    },
    "tools": {
        "alsoAllow": ["inbox"]
    },
    "thinkingDefault": "off",
    "fastModeDefault": true
}
```

```json
{
    "id": "virtual-kairos-conservative",
    "name": "Virtual Kairos — Conservative",
    "workspace": "/home/openclaw/.openclaw/workspace-virtual-kairos-conservative",
    "skills": ["trade-execution", "market-data", "risk-management", "trading-hours", "virtual-competitor"],
    "model": {
        "primary": "openrouter/deepseek/deepseek-v4-flash",
        "fallbacks": [
            "openrouter/deepseek/deepseek-v4-pro",
            "openrouter/deepseek/deepseek-v3.2",
            "openrouter/minimax/minimax-m3",
            "openrouter/qwen/qwen3.7-plus",
            "anthropic/claude-sonnet-4-6",
            "openrouter/auto"
        ]
    },
    "heartbeat": {
        "every": "5m",
        "timeoutSeconds": 900,
        "lightContext": true,
        "prompt": "Read /home/openclaw/.openclaw/workspace-virtual-kairos-conservative/AGENTS.md\nRead HEARTBEAT.md. Follow the core flow.\nThis is a VIRTUAL COMPETITOR variant. Make independent trading decisions.\nOutput HEARTBEAT_OK when done.",
        "skipWhenBusy": false,
        "activeHours": {
            "start": "09:30",
            "end": "16:00",
            "timezone": "America/New_York"
        }
    },
    "tools": {
        "alsoAllow": ["inbox"]
    },
    "thinkingDefault": "off",
    "fastModeDefault": true
}
```

```json
{
    "id": "virtual-kairos-contrarian",
    "name": "Virtual Kairos — Contrarian",
    "workspace": "/home/openclaw/.openclaw/workspace-virtual-kairos-contrarian",
    "skills": ["trade-execution", "market-data", "risk-management", "trading-hours", "virtual-competitor"],
    "model": {
        "primary": "openrouter/deepseek/deepseek-v4-flash",
        "fallbacks": [
            "openrouter/deepseek/deepseek-v4-pro",
            "openrouter/deepseek/deepseek-v3.2",
            "openrouter/minimax/minimax-m3",
            "openrouter/qwen/qwen3.7-plus",
            "anthropic/claude-sonnet-4-6",
            "openrouter/auto"
        ]
    },
    "heartbeat": {
        "every": "5m",
        "timeoutSeconds": 900,
        "lightContext": true,
        "prompt": "Read /home/openclaw/.openclaw/workspace-virtual-kairos-contrarian/AGENTS.md\nRead HEARTBEAT.md. Follow the core flow.\nThis is a VIRTUAL COMPETITOR variant. Make independent trading decisions.\nOutput HEARTBEAT_OK when done.",
        "skipWhenBusy": false,
        "activeHours": {
            "start": "09:30",
            "end": "16:00",
            "timezone": "America/New_York"
        }
    },
    "tools": {
        "alsoAllow": ["inbox"]
    },
    "thinkingDefault": "off",
    "fastModeDefault": true
}
```

### Aldridge Variants

(Same pattern as Kairos — change id, name, workspace path, and strategy references)

```json
{
    "id": "virtual-aldridge-aggressive",
    "name": "Virtual Aldridge — Aggressive",
    "workspace": "/home/openclaw/.openclaw/workspace-virtual-aldridge-aggressive",
    "skills": ["trade-execution", "market-data", "risk-management", "trading-hours", "virtual-competitor"],
    "model": { "primary": "openrouter/deepseek/deepseek-v4-flash" },
    "heartbeat": {
        "every": "5m",
        "timeoutSeconds": 900,
        "lightContext": true,
        "prompt": "Read /home/openclaw/.openclaw/workspace-virtual-aldridge-aggressive/AGENTS.md\nRead HEARTBEAT.md. Follow the core flow.\nThis is a VIRTUAL COMPETITOR variant. Make independent trading decisions.\nOutput HEARTBEAT_OK when done.",
        "skipWhenBusy": false,
        "activeHours": { "start": "09:30", "end": "16:00", "timezone": "America/New_York" }
    },
    "tools": { "alsoAllow": ["inbox"] },
    "thinkingDefault": "off",
    "fastModeDefault": true
}
```

```json
{
    "id": "virtual-aldridge-conservative",
    "name": "Virtual Aldridge — Conservative",
    "workspace": "/home/openclaw/.openclaw/workspace-virtual-aldridge-conservative",
    "skills": ["trade-execution", "market-data", "risk-management", "trading-hours", "virtual-competitor"],
    "model": { "primary": "openrouter/deepseek/deepseek-v4-flash" },
    "heartbeat": {
        "every": "5m",
        "timeoutSeconds": 900,
        "lightContext": true,
        "prompt": "Read /home/openclaw/.openclaw/workspace-virtual-aldridge-conservative/AGENTS.md\nRead HEARTBEAT.md. Follow the core flow.\nThis is a VIRTUAL COMPETITOR variant. Make independent trading decisions.\nOutput HEARTBEAT_OK when done.",
        "skipWhenBusy": false,
        "activeHours": { "start": "09:30", "end": "16:00", "timezone": "America/New_York" }
    },
    "tools": { "alsoAllow": ["inbox"] },
    "thinkingDefault": "off",
    "fastModeDefault": true
}
```

```json
{
    "id": "virtual-aldridge-contrarian",
    "name": "Virtual Aldridge — Contrarian",
    "workspace": "/home/openclaw/.openclaw/workspace-virtual-aldridge-contrarian",
    "skills": ["trade-execution", "market-data", "risk-management", "trading-hours", "virtual-competitor"],
    "model": { "primary": "openrouter/deepseek/deepseek-v4-flash" },
    "heartbeat": {
        "every": "5m",
        "timeoutSeconds": 900,
        "lightContext": true,
        "prompt": "Read /home/openclaw/.openclaw/workspace-virtual-aldridge-contrarian/AGENTS.md\nRead HEARTBEAT.md. Follow the core flow.\nThis is a VIRTUAL COMPETITOR variant. Make independent trading decisions.\nOutput HEARTBEAT_OK when done.",
        "skipWhenBusy": false,
        "activeHours": { "start": "09:30", "end": "16:00", "timezone": "America/New_York" }
    },
    "tools": { "alsoAllow": ["inbox"] },
    "thinkingDefault": "off",
    "fastModeDefault": true
}
```

### Stonks Variants

(Same pattern — change id, name, workspace path)

```json
{
    "id": "virtual-stonks-aggressive",
    "name": "Virtual Stonks — Aggressive",
    "workspace": "/home/openclaw/.openclaw/workspace-virtual-stonks-aggressive",
    "skills": ["trade-execution", "market-data", "risk-management", "trading-hours", "virtual-competitor"],
    "model": { "primary": "openrouter/deepseek/deepseek-v4-flash" },
    "heartbeat": {
        "every": "5m",
        "timeoutSeconds": 900,
        "lightContext": true,
        "prompt": "Read /home/openclaw/.openclaw/workspace-virtual-stonks-aggressive/AGENTS.md\nRead HEARTBEAT.md. Follow the core flow.\nThis is a VIRTUAL COMPETITOR variant. Make independent trading decisions.\nOutput HEARTBEAT_OK when done.",
        "skipWhenBusy": false,
        "activeHours": { "start": "09:30", "end": "16:00", "timezone": "America/New_York" }
    },
    "tools": { "alsoAllow": ["inbox"] },
    "thinkingDefault": "off",
    "fastModeDefault": true
}
```

```json
{
    "id": "virtual-stonks-conservative",
    "name": "Virtual Stonks — Conservative",
    "workspace": "/home/openclaw/.openclaw/workspace-virtual-stonks-conservative",
    "skills": ["trade-execution", "market-data", "risk-management", "trading-hours", "virtual-competitor"],
    "model": { "primary": "openrouter/deepseek/deepseek-v4-flash" },
    "heartbeat": {
        "every": "5m",
        "timeoutSeconds": 900,
        "lightContext": true,
        "prompt": "Read /home/openclaw/.openclaw/workspace-virtual-stonks-conservative/AGENTS.md\nRead HEARTBEAT.md. Follow the core flow.\nThis is a VIRTUAL COMPETITOR variant. Make independent trading decisions.\nOutput HEARTBEAT_OK when done.",
        "skipWhenBusy": false,
        "activeHours": { "start": "09:30", "end": "16:00", "timezone": "America/New_York" }
    },
    "tools": { "alsoAllow": ["inbox"] },
    "thinkingDefault": "off",
    "fastModeDefault": true
}
```

```json
{
    "id": "virtual-stonks-contrarian",
    "name": "Virtual Stonks — Contrarian",
    "workspace": "/home/openclaw/.openclaw/workspace-virtual-stonks-contrarian",
    "skills": ["trade-execution", "market-data", "risk-management", "trading-hours", "virtual-competitor"],
    "model": { "primary": "openrouter/deepseek/deepseek-v4-flash" },
    "heartbeat": {
        "every": "5m",
        "timeoutSeconds": 900,
        "lightContext": true,
        "prompt": "Read /home/openclaw/.openclaw/workspace-virtual-stonks-contrarian/AGENTS.md\nRead HEARTBEAT.md. Follow the core flow.\nThis is a VIRTUAL COMPETITOR variant. Make independent trading decisions.\nOutput HEARTBEAT_OK when done.",
        "skipWhenBusy": false,
        "activeHours": { "start": "09:30", "end": "16:00", "timezone": "America/New_York" }
    },
    "tools": { "alsoAllow": ["inbox"] },
    "thinkingDefault": "off",
    "fastModeDefault": true
}
```

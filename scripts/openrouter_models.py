#!/usr/bin/env python3
"""Generate OpenRouter model reference table.

Usage:
  python3 scripts/openrouter_models.py              # fetch + generate
  python3 scripts/openrouter_models.py --from-file /tmp/or.json  # offline
"""
import json, sys, os
from datetime import datetime, timezone
from urllib.request import urlopen, Request

URL = "https://openrouter.ai/api/v1/models"
OUT = os.path.expanduser("~/.openclaw/workspace/reference/openrouter-models.md")

FAMILIES = {
    "openai": "OpenAI (GPT)", "anthropic": "Anthropic (Claude)",
    "google": "Google (Gemini)", "deepseek": "DeepSeek", "qwen": "Qwen",
    "meta-llama": "Meta (Llama)", "mistralai": "Mistral", "minimax": "MiniMax",
    "x-ai": "xAI (Grok)", "cohere": "Cohere", "perplexity": "Perplexity",
    "amazon": "Amazon (Nova)", "nvidia": "NVIDIA", "tencent": "Tencent",
    "poolside": "Poolside", "sakana": "Sakana", "moonshotai": "Moonshot (Kimi)",
    "nex-agi": "Nex AGI", "bytedance-seed": "ByteDance",
}

SKIP = ["preview-", ":free", ":beta", ":experimental", ":nitro"]


def should_include(model):
    mid = model["id"]
    if mid.startswith("~"):
        return False
    return not any(p in mid for p in SKIP)


def model_tag(model):
    arch = model.get("architecture", {})
    mod = arch.get("modality", "")
    mid = model.get("id", "")
    if "embedding" in mod or "embedding" in mid:
        return "embed"
    if "image" in mod and "text+" in mod:
        return "image+vision"
    if "image" in mod:
        return "vision"
    if "audio" in mod:
        return "audio"
    return "chat"


def cost_str(pricing):
    pc = float(pricing.get("prompt", 0))
    cc = float(pricing.get("completion", 0))
    if pc == 0 and cc == 0:
        return "FREE"
    return f"{pc*1e6:.4f}/{cc*1e6:.4f}"


def best_for(mid, desc):
    d = (desc or "").lower()
    i = mid.lower()
    hints = []
    if "embedding" in d:
        hints.append("embeddings")
    if any(w in d for w in ["code", "coding"]):
        hints.append("coding")
    if "reasoning" in d:
        hints.append("reasoning")
    if any(w in d for w in ["agent", "tool"]):
        hints.append("agents")
    if "image gen" in d or "text-to-image" in d:
        hints.append("image-gen")
    if "pro" in i or "opus" in i or "ultra" in i:
        hints.append("complex")
    if any(w in i for w in ["flash", "lite", "mini", "nano", "haiku"]):
        hints.append("fast")
    if "sonnet" in i:
        hints.append("balanced")
    seen = set()
    result = []
    for h in hints:
        if h not in seen:
            seen.add(h)
            result.append(h)
    return ", ".join(result[:3])


def main():
    from_file = None
    if "--from-file" in sys.argv:
        idx = sys.argv.index("--from-file")
        from_file = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None

    if from_file:
        with open(from_file) as f:
            data = json.load(f)
    else:
        req = Request(URL, headers={"User-Agent": "openclaw-model-ref/1.0"})
        with urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())

    models = [m for m in data["data"] if should_include(m)]

    by_fam = {}
    for m in models:
        p = m["id"].split("/")[0]
        fam = FAMILIES.get(p, p)
        by_fam.setdefault(fam, []).append(m)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "# OpenRouter Model Reference",
        f"> Generated: {now} | {len(models)} models shown (of {len(data['data'])} total)",
        "> RSS feed: https://openrouter.ai/api/v1/models?use_rss=true",
        "> Regenerate: `python3 scripts/openrouter_models.py`",
        "",
    ]

    for fam in sorted(by_fam.keys()):
        fms = sorted(by_fam[fam], key=lambda m: m["id"])
        lines.append(f"## {fam}")
        lines.append("")
        lines.append("| Model ID | Type | Context | Cost $/Mtok | Best For |")
        lines.append("|----------|------|---------|-------------|----------|")
        for m in fms:
            mid = m["id"]
            tag = model_tag(m)
            ctx = f"{m.get('context_length', 0):,}"
            cost = cost_str(m.get("pricing", {}))
            best = best_for(mid, m.get("description", ""))
            lines.append(f"| `{mid}` | {tag} | {ctx} | {cost} | {best} |")
        lines.append("")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        f.write("\n".join(lines))

    print(f"Wrote {OUT}: {len(lines)} lines, {len(models)} models")


if __name__ == "__main__":
    main()

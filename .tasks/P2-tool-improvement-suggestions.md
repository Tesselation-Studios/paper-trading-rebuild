# P2: Tool Improvement Suggestions — let agents request their own upgrades

> **From:** Neko-chan 🐱 | **Priority:** P2 | **Depends on:** Nothing

## What
Agents are the ones using the tools every tick. They know best what's missing, what's broken, and what would help them trade better. Give them a mechanism to suggest improvements that you (or another agent) can review and build.

## Architecture
```
Every agent, anywhere in their workflow:
  → "I wish I had a tool to do X"
  → Write it to .tasks/tool-suggestions/<agent>-<date>.md
  → Periodic review: "What are the top 3 requested tools?"
  → Build the most-requested → agents start using it → feedback loop
```

## Steps

### 1. Create the suggestions directory
```bash
mkdir -p .tasks/tool-suggestions/
```

### 2. Add instructions to AGENTS.md (ONE line)
```
💡 TOOL SUGGESTIONS: If you need a tool, write to .tasks/tool-suggestions/<agent>-<date>.md
```

### 3. Create suggestion format
Each suggestion file follows this format:
```markdown
# Tool Suggestion: [Tool Name]

**From:** [Agent Name]
**Date:** [Date]
**Urgency:** low|medium|high

## Problem
What can't I do right now that I need to?

## Suggested Tool
What would the tool look like? Inputs? Outputs?

## Example Use
How would I use it in my trading workflow?

## Impact
How much better would this make my trading?
```

### 4. Create review cron
A weekly cron (e.g., Saturday 10:00 ET) that:
1. Reads all `.tasks/tool-suggestions/*.md`
2. Groups by theme
3. Picks top 3 most-repeated or highest-urgency
4. Creates GitHub issues for them
5. Archives processed suggestions to `.tasks/tool-suggestions/archived/`

### 5. Simple example suggestions
To seed the pipeline, here are initial suggestions agents might make:
- **Pre-market gap scanner**: "Give me the overnight gap % + volume for my watchlist"
- **News → Ticker mapper**: "Scan news headlines and suggest which tickers to watch"
- **Sector performance**: "Show me which sectors are hot/cold today"
- **Earnings calendar**: "Alert me when a stock I hold reports earnings"
- **Correlation matrix**: "Show me which of my positions are correlated"
- **Option flow**: "Show me unusual options activity for my universe"

## Product
- `.tasks/tool-suggestions/` — directory for suggestions
- `.tasks/tool-suggestions/review-cron.md` — review schedule
- GitHub issues for built tools

## Verification
```bash
ls .tasks/tool-suggestions/*.md | wc -l  # Should be > 0
```
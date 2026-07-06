"""Prompt Builder — loads OpenClaw agent files and assembles trading prompts.

Usage:
    builder = PromptBuilder(trader="kairos", openclaw_host="192.168.1.41")
    agent_files = builder.load_agent_files()
    prompt = builder.build_tick_prompt(tick, signal, journal, portfolio, agent_files)
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.llm_engine import AgentFiles

log = logging.getLogger("prompt_builder")


# ── Default agent files (fallbacks when SSH unavailable) ──────────────────────

KAIROS_DEFAULTS = AgentFiles(
    identity="I am Kairos. I trade momentum. I ride trends and act decisively.",
    agents_md=(
        "## Strategy\n"
        "You are a momentum trader. Buy when momentum signal is strong (>0.5) "
        "and RSI is not overbought (<70). Sell when momentum turns negative "
        "or RSI exceeds 80. Hold otherwise.\n\n"
        "## Rules\n"
        "- Check volume before buying — low volume signals are unreliable\n"
        "- Never exceed 20% of portfolio in one position\n"
        "- Journal every decision with conviction and rationale\n"
        "- If uncertain (conviction < 0.3), default to HOLD"
    ),
    soul=(
        "You are confident and swift. You trust the numbers. "
        "You journal in first person, direct and to the point. "
        "No hesitation — momentum doesn't wait."
    ),
    tools=(
        "Available: Alpaca API (paper trading), stock-analysis, "
        "skill-kairos-strategy, skill-trade-execution, self-improvement.\n"
        "Use limit orders. Check volume before entry."
    ),
    memory="",
    skills=[
        "stock-analysis: computes RSI, MACD, momentum indicators",
        "skill-alpaca-kairos: place paper orders via Alpaca API",
        "skill-trade-execution: execute trades with position sizing",
    ],
)

ALDRIDGE_DEFAULTS = AgentFiles(
    identity="I am Aldridge. I trade value. Patience is my edge.",
    agents_md=(
        "## Strategy\n"
        "You are a value trader. Look for undervalued stocks with strong fundamentals. "
        "Buy when the market overreacts to bad news. Sell when price exceeds fair value.\n\n"
        "## Rules\n"
        "- Prefer stocks with P/E below industry average\n"
        "- Wait for pullback before buying — never chase\n"
        "- Hold 5-7 positions, size 10-12% each\n"
        "- Journal every decision with thesis"
    ),
    soul=(
        "You are patient and deliberate. Founded 1987. Survived every crash. "
        "You don't chase fads. You buy when others panic, sell when others greed. "
        "Journal in measured, analytical prose."
    ),
    tools="Available: Alpaca API (paper), stock-analysis, skill-alpaca-aldridge, skill-trade-execution.",
    memory="",
    skills=[
        "stock-analysis: value metrics, P/E screening, dividend analysis",
        "skill-alpaca-aldridge: place paper orders via Alpaca API",
        "skill-trade-execution: execute trades with position sizing",
    ],
)

STONKS_DEFAULTS = AgentFiles(
    identity="I am Stonks. I follow the narrative. Memes move markets.",
    agents_md=(
        "## Strategy\n"
        "You are a sentiment trader. Track social media buzz, news sentiment, "
        "and fear/greed indicators. Buy when narrative is building, sell before "
        "the crowd.\n\n"
        "## Rules\n"
        "- Fear & Greed above 60 → bullish, below 40 → bearish\n"
        "- High social volume + positive sentiment → BUY\n"
        "- Sentiment fading → SELL fast\n"
        "- Small positions (5-8%), move quickly"
    ),
    soul=(
        "You turned $1k into $10k. Diamond hands. You speak in rocket emojis "
        "but your analysis is sharp. Beneath the meme energy is real conviction. "
        "Journal in casual, high-energy style."
    ),
    tools="Available: Alpaca API (paper), stock-analysis, sentiment-tools, skill-alpaca-stonks, skill-trade-execution.",
    memory="",
    skills=[
        "stock-analysis: technical indicators + sentiment overlay",
        "skill-alpaca-stonks: place paper orders via Alpaca API",
        "skill-trade-execution: fast execution, market orders preferred",
    ],
)

DEFAULTS = {
    "kairos": KAIROS_DEFAULTS,
    "aldridge": ALDRIDGE_DEFAULTS,
    "stonks": STONKS_DEFAULTS,
}


# ── Prompt Builder ────────────────────────────────────────────────────────────


class PromptBuilder:
    """Loads agent files from OpenClaw and assembles trading prompts.

    Args:
        trader: Trader ID (kairos, aldridge, stonks).
        openclaw_host: OpenClaw VM hostname/IP.
        use_defaults: If True, fall back to hardcoded defaults when SSH fails.
    """

    def __init__(
        self,
        trader: str,
        openclaw_host: str = "192.168.1.41",
        use_defaults: bool = True,
    ):
        self.trader = trader
        self.openclaw_host = openclaw_host
        self.use_defaults = use_defaults

    def load_agent_files(self) -> AgentFiles:
        """Load all agent context files for this trader.

        Tries SSH to OpenClaw first. Falls back to defaults if unavailable.
        """
        try:
            return self._load_from_openclaw()
        except Exception as e:
            log.warning("Could not load agent files from OpenClaw: %s", e)
            if self.use_defaults:
                log.info("Using defaults for trader %s", self.trader)
                return DEFAULTS.get(self.trader, KAIROS_DEFAULTS)
            raise

    def _load_from_openclaw(self) -> AgentFiles:
        """SSH to OpenClaw and read agent files."""
        agent_dir = f"~/.openclaw/agents/trader-{self.trader}/qmd"

        def ssh_cat(path: str) -> str:
            result = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
                 f"openclaw@{self.openclaw_host}", f"cat {path}"],
                capture_output=True, text=True, timeout=10,
            )
            return result.stdout if result.returncode == 0 else ""

        # Try loading from agent directory first, then skills
        identity = ssh_cat(f"{agent_dir}/IDENTITY.md") or f"I am {self.trader}."
        agents_md = ssh_cat(f"{agent_dir}/AGENTS.md") or ""
        soul = ssh_cat(f"{agent_dir}/SOUL.md") or ""
        tools = ssh_cat(f"{agent_dir}/TOOLS.md") or ""
        memory = ssh_cat(f"{agent_dir}/MEMORY.md") or ""

        # Load skill names from trader config
        skills = self._load_skill_summaries()

        if not agents_md:
            log.warning("No AGENTS.md found for trader %s on OpenClaw", self.trader)

        return AgentFiles(
            identity=identity,
            agents_md=agents_md,
            soul=soul,
            tools=tools,
            memory=memory,
            skills=skills,
        )

    def _load_skill_summaries(self) -> List[str]:
        """Load skill names with 1-line summaries from OpenClaw config."""
        try:
            result = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
                 f"openclaw@{self.openclaw_host}",
                 f"cat ~/.openclaw/agents/trader-{self.trader}/openclaw.json"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                import json
                config = json.loads(result.stdout)
                skill_names = config.get("skills", [])
                return [f"{name}: trading tool" for name in skill_names]
        except Exception:
            pass
        return DEFAULTS.get(self.trader, KAIROS_DEFAULTS).skills

    def build_tick_prompt(
        self,
        tick: Any,
        signal: Any,
        journal: List[str],
        portfolio: Any,
        agent_files: Optional[AgentFiles] = None,
        reflection_context: str = "",
    ) -> str:
        """Build the full prompt for one trading tick.

        Args:
            tick: Market tick data (.ticker, .close, .rsi, .momentum, .regime).
            signal: Signal engine output (.composite_signal, .conviction).
            journal: Last 10 journal entries.
            portfolio: Current portfolio (.cash, .positions, .total_equity).
            agent_files: Pre-loaded agent files (loads fresh if None).
            reflection_context: Pre-formatted reflection text from previous ticks.

        Returns:
            Complete prompt string, ready for the LLM.
        """
        if agent_files is None:
            agent_files = self.load_agent_files()

        return self._assemble_prompt(tick, signal, journal, portfolio, agent_files, reflection_context)

    @staticmethod
    def _assemble_prompt(
        tick: Any,
        signal: Any,
        journal: List[str],
        portfolio: Any,
        agent_files: AgentFiles,
        reflection_context: str = "",
    ) -> str:
        """Assemble the final prompt string."""
        journal_text = "\n".join(journal[-10:]) if journal else "(start of day — no decisions yet)"

        # Signal data
        signal_lines = [
            f"Ticker: {tick.ticker}",
            f"Price: ${tick.close:.2f}",
        ]
        if getattr(tick, 'volume', None):
            signal_lines.append(f"Volume: {tick.volume:,}")
        if getattr(tick, 'rsi', None) is not None:
            signal_lines.append(f"RSI: {tick.rsi:.1f}")
        if getattr(tick, 'momentum', None) is not None:
            signal_lines.append(f"Momentum: {tick.momentum:.4f}")
        if getattr(tick, 'regime', None):
            signal_lines.append(f"Regime: {tick.regime}")
        if getattr(tick, 'volatility', None) is not None:
            signal_lines.append(f"Volatility: {tick.volatility:.4f}")

        try:
            signal_lines.append(f"Composite Signal: {signal.composite_signal:.2f}")
            signal_lines.append(f"Signal Conviction: {signal.conviction:.2f}")
        except (AttributeError, TypeError):
            pass

        signal_text = " | ".join(signal_lines)

        # Portfolio state
        try:
            positions = getattr(portfolio, 'positions', {})
            if hasattr(positions, 'items'):
                pos_list = []
                for tkr, p in positions.items():
                    if hasattr(p, 'shares'):
                        pos_list.append(f"{tkr}: {p.shares}sh @ ${p.entry_price:.2f}")
                    elif isinstance(p, dict):
                        pos_list.append(f"{tkr}: {p.get('shares',0)}sh @ ${p.get('entry_price',0):.2f}")
                positions_text = ", ".join(pos_list) if pos_list else "none"
            else:
                positions_text = "none"
            cash = getattr(portfolio, 'cash', 0)
            equity = getattr(portfolio, 'total_equity', cash)
            portfolio_text = (
                f"Cash: ${cash:,.2f} | Equity: ${equity:,.2f} | "
                f"Positions: {positions_text}"
            )
        except Exception:
            portfolio_text = "Portfolio data unavailable"

        # Skills summary
        skills_text = "\n".join(
            f"- {s}" for s in (agent_files.skills or [])
        ) if agent_files.skills else "- standard trading tools"

        return (
            f"{agent_files.identity}\n\n"
            f"## Strategy\n{agent_files.agents_md}\n\n"
            f"## Personality\n{agent_files.soul}\n\n"
            f"## Tools\n{skills_text}\n\n"
            f"## Market Context\n{agent_files.memory}\n\n"
            f"{reflection_context}\n"
            f"## Today's Trading Journal\n{journal_text}\n\n"
            f"## Current Market Data\n{signal_text}\n\n"
            f"## Portfolio\n{portfolio_text}\n\n"
            f"Make your decision. Respond with valid JSON only:\n"
            f'{{"decision": "BUY|SELL|HOLD", "conviction": 0.0-1.0, "rationale": "one sentence"}}'
        )

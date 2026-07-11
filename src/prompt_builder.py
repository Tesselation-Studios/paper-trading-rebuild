"""Prompt Builder — loads agent files and assembles trading prompts.

Usage:
    builder = PromptBuilder(trader="kairos")
    agent_files = builder.load_agent_files()
    prompt = builder.build_tick_prompt(tick, signal, journal, portfolio, agent_files)

Architecture:
    Phase 0 (historical sim): reads from local agents/trader-{name}/ directory.
    No SSH. Falls back to hardcoded defaults if local files are missing.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.agent_files import AgentFiles

log = logging.getLogger("prompt_builder")


# ── Default agent files (fallbacks when local files unavailable) ─────────────

KAIROS_DEFAULTS = AgentFiles(
    identity="I am Kairos. I trade momentum. I ride trends and act decisively.",
    agents_md=(
        "## Strategy\n"
        "You are a momentum trader. Buy when momentum signal is strong (>0.5) "
        "and RSI is not overbought (<70). Sell when momentum turns negative "
        "or RSI exceeds 80. Hold otherwise.\n\n"
        "## Rules\n"
        "- Volume filter: only enter when volume >= 1.2x 20-day average\n"
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
        "- Fear & Greed above 60 -> bullish, below 40 -> bearish\n"
        "- High social volume + positive sentiment -> BUY\n"
        "- Sentiment fading -> SELL fast\n"
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

DEFAULTS: Dict[str, AgentFiles] = {
    "kairos": KAIROS_DEFAULTS,
    "aldridge": ALDRIDGE_DEFAULTS,
    "stonks": STONKS_DEFAULTS,
}

# Path to the local agents directory (relative to this source file)
AGENTS_DIR = Path(__file__).resolve().parent.parent / "agents"


# ── Prompt Builder ────────────────────────────────────────────────────────────


class PromptBuilder:
    """Loads agent files and assembles trading prompts.

    Phase 0 / historical-sim mode: reads from local ``agents/trader-{name}/``
    directory. Legacy SSH fallback (OpenClaw VM) still available but not the
    default path.

    Args:
        trader: Trader ID (kairos, aldridge, stonks).
        source: ``"local"`` (default, reads filesystem), ``"ssh"`` (OpenClaw VM),
                or ``"defaults"`` (embedded constants only).
        openclaw_host: OpenClaw VM hostname/IP (only used when source="ssh").
        use_defaults: If True, fall back to hardcoded defaults when local/SSH
                      files are unavailable.
    """

    def __init__(
        self,
        trader: str,
        source: str = "local",
        openclaw_host: str = "192.168.1.41",
        use_defaults: bool = True,
    ):
        if source not in ("local", "ssh", "defaults"):
            raise ValueError(f"Unknown source: {source!r}. Choose from: local, ssh, defaults")
        self.trader = trader
        self.source = source
        self.openclaw_host = openclaw_host
        self.use_defaults = use_defaults

    # ── Public API ────────────────────────────────────────────────────────

    def load_agent_files(self) -> AgentFiles:
        """Load all agent context files for this trader.

        Resolution order (depending on ``source``):
        1. ``"local"`` (default) — reads from ``agents/trader-{name}/``
        2. ``"ssh"`` — reads from OpenClaw VM via subprocess
        3. ``"defaults"`` — embedded constants only

        Falls back to hardcoded defaults when ``use_defaults=True`` and the
        preferred source is unavailable.
        """
        if self.source == "defaults":
            return self._get_fallback()

        if self.source == "ssh":
            return self._load_with_fallback(self._load_from_openclaw)

        # "local" — Phase 0 default
        return self._load_with_fallback(self._load_from_local)

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

        return self._assemble_prompt(tick, signal, journal, portfolio,
                                     agent_files, reflection_context)

    # ── Local filesystem loader (Phase 0: no SSH) ─────────────────────────

    def _trader_dir(self) -> Path:
        """Path to ``agents/trader-{name}/``."""
        return AGENTS_DIR / f"trader-{self.trader}"

    def _load_from_local(self) -> AgentFiles:
        """Read agent files from the local ``agents/trader-{name}/`` directory.

        Expected files:
            AGENTS.md — the operating manual / strategy prompt
            SOUL.md   — persona / personality
            TOOLS.md  — available tools (optional)
            MEMORY.md — persistent learnings (optional)
            config.yaml — skill config (optional)

        Identity is derived from the first line of SOUL.md or trader name.
        """
        agent_dir = self._trader_dir()
        if not agent_dir.is_dir():
            raise FileNotFoundError(
                f"Trader directory not found: {agent_dir}"
            )

        agents_md = self._read_local_file(agent_dir / "AGENTS.md")
        soul = self._read_local_file(agent_dir / "SOUL.md")
        tools = self._read_local_file(agent_dir / "TOOLS.md")
        memory = self._read_local_file(agent_dir / "MEMORY.md")
        prompt = self._read_local_file(agent_dir / "prompt.txt")

        # If no AGENTS.md but prompt.txt exists, use prompt.txt as agents_md
        if not agents_md and prompt:
            agents_md = prompt
            log.info("Using prompt.txt as agents_md for trader %s", self.trader)

        # Derive identity from the first substantive line of SOUL.md
        identity = self._derive_identity(soul)

        # Load skills from config.yaml
        skills = self._load_skills_from_config(agent_dir / "config.yaml")

        if not agents_md:
            log.warning("No AGENTS.md or prompt.txt found for trader %s",
                        self.trader)

        return AgentFiles(
            identity=identity,
            agents_md=agents_md or "",
            soul=soul or "",
            tools=tools or "",
            memory=memory or "",
            skills=skills,
        )

    # ── SSH loader (legacy — OpenClaw VM) ─────────────────────────────────

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

        identity = ssh_cat(f"{agent_dir}/IDENTITY.md") or f"I am {self.trader}."
        agents_md = ssh_cat(f"{agent_dir}/AGENTS.md") or ""
        soul = ssh_cat(f"{agent_dir}/SOUL.md") or ""
        tools = ssh_cat(f"{agent_dir}/TOOLS.md") or ""
        memory = ssh_cat(f"{agent_dir}/MEMORY.md") or ""

        skills = self._load_skill_summaries_ssh()

        if not agents_md:
            log.warning("No AGENTS.md found for trader %s on OpenClaw",
                        self.trader)

        return AgentFiles(
            identity=identity,
            agents_md=agents_md,
            soul=soul,
            tools=tools,
            memory=memory,
            skills=skills,
        )

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _read_local_file(path: Path) -> str:
        """Read a local text file. Returns empty string on error."""
        try:
            return path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return ""

    def _derive_identity(self, soul_text: str) -> str:
        """Extract a short identity line from the first contentful line of SOUL.md."""
        for line in soul_text.strip().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                cleaned = stripped.lstrip("*_ `").strip()
                if cleaned:
                    if len(cleaned) <= 100:
                        return cleaned
                    # Try first sentence/clause
                    import re
                    first_sentence = re.split(r'[.\n]', cleaned)[0]
                    if first_sentence and len(first_sentence) <= 100:
                        return first_sentence.strip()
                    return cleaned[:100].rstrip(",; ") + "..."
        return f"I am {self.trader}."

    def _load_skills_from_config(self, config_path: Path) -> List[str]:
        """Read skills from a trader's config.yaml.

        Extracts the model primary field and any strategy-related fields,
        formatting them as skill summaries.
        """
        try:
            if not config_path.is_file():
                return DEFAULTS.get(self.trader, KAIROS_DEFAULTS).skills or []

            import yaml
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if not isinstance(config, dict):
                return DEFAULTS.get(self.trader, KAIROS_DEFAULTS).skills or []

            skills: List[str] = []

            # Model info
            model_primary = config.get("model", {}).get("primary", "openrouter")
            skills.append(f"LLM engine: {model_primary}")

            # Strategy
            strategy = config.get("strategy", "trading")
            skills.append(f"strategy: {strategy}")

            # Signals
            sig = config.get("signals", {})
            if sig:
                conf = sig.get("minimum_confidence", 0.5)
                skills.append(f"minimum confidence threshold: {conf}")
                confirm = sig.get("confirmations_required", 2)
                skills.append(f"confirmations required: {confirm}")

            # Risk
            risk = config.get("risk", {})
            if risk:
                skills.append(
                    f"max position: {risk.get('max_position_pct', 10) * 100:.0f}% | "
                    f"stop loss: {risk.get('stop_loss_pct', 5) * 100:.0f}% | "
                    f"max daily loss: ${risk.get('max_daily_loss', 500)}"
                )

            return skills if skills else DEFAULTS.get(self.trader, KAIROS_DEFAULTS).skills or []
        except Exception as e:
            log.debug("Failed to parse config for %s: %s", self.trader, e)
            return DEFAULTS.get(self.trader, KAIROS_DEFAULTS).skills or []

    def _load_skill_summaries_ssh(self) -> List[str]:
        """Load skill names from OpenClaw config via SSH."""
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
        return DEFAULTS.get(self.trader, KAIROS_DEFAULTS).skills or []

    def _get_fallback(self) -> AgentFiles:
        """Return the hardcoded defaults for this trader."""
        return DEFAULTS.get(self.trader, KAIROS_DEFAULTS)

    def _load_with_fallback(self, loader_fn) -> AgentFiles:
        """Try a loader function; fall back to defaults on failure."""
        try:
            return loader_fn()
        except Exception as e:
            log.warning("Could not load agent files for %s via %s: %s",
                        self.trader, loader_fn.__name__, e)
            if self.use_defaults:
                log.info("Falling back to defaults for trader %s", self.trader)
                return self._get_fallback()
            raise

    # ── Prompt assembly ──────────────────────────────────────────────────

    @staticmethod
    def _format_signal_report(tick: Any, signal: Any) -> str:
        """Format market data + signal report into a compact text block."""
        lines = [
            f"Ticker: {tick.ticker}",
            f"Price: ${tick.close:.2f}",
        ]
        if getattr(tick, "volume", None):
            lines.append(f"Volume: {tick.volume:,}")
        if getattr(tick, "rsi", None) is not None:
            lines.append(f"RSI: {tick.rsi:.1f}")
        if getattr(tick, "momentum", None) is not None:
            lines.append(f"Momentum: {tick.momentum:.4f}")
        if getattr(tick, "regime", None):
            lines.append(f"Regime: {tick.regime}")
        if getattr(tick, "volatility", None) is not None:
            lines.append(f"Volatility: {tick.volatility:.4f}")

        if signal is not None:
            if hasattr(signal, "momentum_score"):
                sr = signal  # SignalReport
                lines.append(f"Momentum: {sr.momentum_score:.3f} ({sr.momentum_signal})")
                lines.append(f"RSI Signal: {sr.rsi_signal} ({sr.rsi:.1f})")
                lines.append(f"Volatility Regime: {sr.volatility_regime}")
                lines.append(f"Market Regime: {sr.regime} (conf:{sr.regime_confidence:.2f})")
                lines.append(f"Volume Ratio: {sr.volume_ratio or 0:.2f}x avg")
                lines.append(f"Composite Signal: {sr.composite_signal:.3f}")
                lines.append(f"Conviction: {sr.conviction:.3f}")
                lines.append(f"Suggested Size: {sr.recommended_size_pct:.1%} of equity")
                lines.append(f"Stop Loss: ${sr.stop_loss:.2f}")
                lines.append(f"Take Profit: ${sr.take_profit:.2f}")
            else:
                try:
                    lines.append(f"Composite Signal: {signal.composite_signal:.2f}")
                    lines.append(f"Signal Conviction: {signal.conviction:.2f}")
                except (AttributeError, TypeError):
                    pass

        return " | ".join(lines)

    @staticmethod
    def _format_portfolio(portfolio: Any) -> str:
        """Format portfolio state into a compact text block."""
        try:
            positions = getattr(portfolio, "positions", {})
            if hasattr(positions, "items"):
                pos_list = []
                for tkr, p in positions.items():
                    if hasattr(p, "shares"):
                        pos_list.append(
                            f"{tkr}: {p.shares}sh @ ${p.entry_price:.2f} "
                            f"(now ${getattr(p, 'current_price', p.entry_price):.2f})"
                        )
                    elif isinstance(p, dict):
                        pos_list.append(
                            f"{tkr}: {p.get('shares', 0)}sh @ "
                            f"${p.get('entry_price', 0):.2f}"
                        )
                positions_text = ", ".join(pos_list) if pos_list else "none"
            else:
                positions_text = "none"
            cash = getattr(portfolio, "cash", 0)
            equity = getattr(portfolio, "total_equity", cash)
            n_pos = getattr(portfolio, "position_count",
                            len(positions) if hasattr(positions, "items") else 0)
            return (
                f"Cash: ${cash:,.2f} | "
                f"Equity: ${equity:,.2f} | "
                f"Positions ({n_pos}): {positions_text}"
            )
        except Exception:
            return "Portfolio data unavailable"

    @staticmethod
    def _format_skills(agent_files: AgentFiles) -> str:
        """Format skills summary."""
        if agent_files.skills:
            return "\n".join(f"- {s}" for s in agent_files.skills)
        return "- standard trading tools"

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
        journal_text = "\n".join(journal[-10:]) if journal else (
            "(start of day — no decisions yet)")
        signal_text = PromptBuilder._format_signal_report(tick, signal)
        portfolio_text = PromptBuilder._format_portfolio(portfolio)
        skills_text = PromptBuilder._format_skills(agent_files)

        return (
            f"{agent_files.agents_md}\n\n"
            f"## Personality\n{agent_files.soul}\n\n"
            f"## Available Tools\n{skills_text}\n\n"
            f"## Market Memory\n{agent_files.memory}\n\n"
            f"{reflection_context}\n"
            f"## Today's Decisions\n{journal_text}\n\n"
            f"## Current Market Data\n{signal_text}\n\n"
            f"## Portfolio\n{portfolio_text}\n\n"
            f"Respond with JSON: "
            f'{{"decision": "BUY|SELL|HOLD", '
            f'"conviction": 0.0-1.0, "rationale": "..."}}'
        )
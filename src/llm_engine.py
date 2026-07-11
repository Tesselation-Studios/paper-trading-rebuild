"""LLM Engine — direct OpenRouter API calls for simulation ticks.

No OpenClaw agent dispatch. Just HTTP to the model. Fast, cheap, parallelizable.

Usage:
    engine = LLMEngine(model="openrouter/deepseek/deepseek-v4-flash")
    decision = engine.decide(tick, signal, journal, portfolio, agent_files)
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.replay import Tick, Portfolio, TraderDecision
from src.prompt_builder import PromptBuilder
from src.agent_files import AgentFiles

# Forward ref for type hints (avoid circular imports)
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.reflection import Reflection

log = logging.getLogger("llm_engine")


# ── Config ────────────────────────────────────────────────────────────────────

def _load_api_key() -> str:
    """Load OpenRouter API key from env or .env files."""
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if key:
        return key
    # Try multiple .env locations (Hermes on Mac, Casper on VM)
    for env_dir in [".hermes", ".openclaw", ""]:
        env_path = Path.home() / env_dir / ".env" if env_dir else Path.home() / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("OPENROUTER_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("OPENROUTER_API_KEY not found in env or ~/.openclaw/.env, ~/.hermes/.env, ~/.env")


# ── Engine ────────────────────────────────────────────────────────────────────


class LLMEngine:
    """Calls OpenRouter API for trading decisions.

    Args:
        model: OpenRouter model string (e.g. 'openrouter/deepseek/deepseek-v4-flash').
        api_key: OpenRouter API key. If None, loaded from env.
        base_url: API base URL.
        temperature: LLM temperature (0.3 = moderately deterministic).
        max_tokens: Max response tokens.
        timeout: HTTP timeout in seconds.
        max_retries: Retry on transient errors.
    """

    BASE_URL = "https://openrouter.ai/api/v1"
    SYSTEM_PROMPT = (
        "You are a paper trading agent. Respond ONLY with valid JSON. "
        "No explanations outside the JSON. No markdown fences. Just the JSON object."
    )

    def __init__(
        self,
        model: str = "deepseek/deepseek-v4-flash",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 2000,
        timeout: int = 30,
        max_retries: int = 2,
    ):
        self.model = model
        self.api_key = api_key or _load_api_key()
        self.base_url = (base_url or self.BASE_URL).rstrip("/")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.max_retries = max_retries

    def decide(
        self,
        tick: Tick,
        signal: Any,  # SignalReport
        journal: List[str],
        portfolio: Portfolio,
        agent_files: AgentFiles,
        reflection_context: str = "",
    ) -> TraderDecision:
        """Ask the LLM for a trading decision.

        Args:
            tick: Current market tick.
            signal: Signal engine output (has .composite_signal, .conviction, .regime).
            journal: Last entries from today's trading.
            portfolio: Current portfolio state.
            agent_files: Agent context files.
            reflection_context: Pre-formatted reflection context from previous ticks.

        Returns:
            TraderDecision — BUY, SELL, or HOLD.
        """
        prompt = self._build_prompt(tick, signal, journal, portfolio, agent_files, reflection_context)
        response = self._call_api(prompt)
        return self._parse_response(response, tick)

    def reflect(
        self,
        tick: Tick,
        decision: TraderDecision,
        signal: Any,
        prev_reflections: Optional[List] = None,
    ) -> tuple[str, str]:
        """Ask the LLM 'what did you learn from this decision?'

        Uses a reflection-specific prompt to extract insights from the trading
        decision. Returns (learning, would_do_differently) strings.

        Args:
            tick: Current market tick.
            decision: The trader's decision at this tick.
            signal: Signal engine output (may be None).
            prev_reflections: Previous Reflection objects for context.

        Returns:
            Tuple of (learning: str, would_do_differently: str).
        """
        prompt = self._build_reflection_prompt(tick, decision, signal, prev_reflections)
        response = self._call_api(prompt)
        return self._parse_reflection_response(response)

    def _build_prompt(
        self,
        tick: Tick,
        signal: Any,
        journal: List[str],
        portfolio: Portfolio,
        agent_files: AgentFiles,
        reflection_context: str = "",
    ) -> str:
        """Assemble the full prompt from agent files + data + journal + reflections.

        Delegates to PromptBuilder for the canonical prompt assembly.
        """
        return PromptBuilder._assemble_prompt(
            tick=tick,
            signal=signal,
            journal=journal,
            portfolio=portfolio,
            agent_files=agent_files,
            reflection_context=reflection_context,
        )

    def _call_api(self, prompt: str) -> Optional[str]:
        """Call OpenRouter API with retry logic."""
        url = f"{self.base_url}/chat/completions"
        body = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }).encode()

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                req = urllib.request.Request(url, data=body, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    result = json.loads(resp.read())
                    content = result["choices"][0]["message"]["content"]
                    log.debug("LLM response (%dms): %.100s...",
                              int((time.monotonic() - getattr(self, '_t0', time.monotonic())) * 1000),
                              content)
                    return content
            except urllib.error.HTTPError as e:
                last_error = f"HTTP {e.code}: {e.read().decode()[:200]}"
                log.warning("OpenRouter attempt %d/%d: %s", attempt + 1, self.max_retries + 1, last_error)
                if e.code == 429:  # rate limit
                    time.sleep(2 ** attempt)
            except Exception as e:
                last_error = str(e)
                log.warning("OpenRouter attempt %d/%d: %s", attempt + 1, self.max_retries + 1, last_error)
                time.sleep(0.5)

        log.error("OpenRouter failed after %d retries: %s", self.max_retries, last_error)
        return None

    def _parse_response(self, content: Optional[str], tick: Tick) -> TraderDecision:
        """Parse LLM JSON response into TraderDecision. Safe fallback to HOLD."""
        if not content:
            return TraderDecision(
                ticker=tick.ticker, decision="HOLD", conviction=0.0,
                rationale="API error — defaulting to HOLD",
            )

        # Strip markdown code fences if present
        content = re.sub(r'^```(?:json)?\s*', '', content.strip())
        content = re.sub(r'\s*```$', '', content)

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            # Try to extract JSON object from text
            match = re.search(r'\{[^{}]*"decision"[^{}]*\}', content)
            if match:
                try:
                    data = json.loads(match.group())
                except json.JSONDecodeError:
                    return TraderDecision(
                        ticker=tick.ticker, decision="HOLD", conviction=0.0,
                        rationale=f"Unparseable response: {content[:100]}",
                    )
            else:
                return TraderDecision(
                    ticker=tick.ticker, decision="HOLD", conviction=0.0,
                    rationale=f"No JSON found: {content[:100]}",
                )

        decision = data.get("decision", "HOLD").upper()
        if decision not in ("BUY", "SELL", "HOLD"):
            decision = "HOLD"

        conviction = float(data.get("conviction", 0.0))
        conviction = max(0.0, min(1.0, conviction))

        rationale = str(data.get("rationale", ""))[:200]

        return TraderDecision(
            ticker=tick.ticker,
            decision=decision,
            conviction=conviction,
            rationale=rationale,
        )

    def _build_reflection_prompt(
        self,
        tick: Tick,
        decision: TraderDecision,
        signal: Any,
        prev_reflections: Optional[List] = None,
    ) -> str:
        """Build the reflection prompt asking 'what did I learn?'"""
        # Signal summary
        sig_lines = [f"Price: ${tick.close:.2f}"]
        if tick.rsi is not None:
            sig_lines.append(f"RSI: {tick.rsi:.1f}")
        if tick.momentum is not None:
            sig_lines.append(f"Momentum: {tick.momentum:.4f}")
        if tick.regime:
            sig_lines.append(f"Regime: {tick.regime}")
        if signal is not None:
            try:
                sig_lines.append(f"Composite: {signal.composite_signal:.2f}")
                sig_lines.append(f"Conviction: {signal.conviction:.2f}")
            except (AttributeError, TypeError):
                pass
        signal_text = " | ".join(sig_lines)

        # Previous reflections context (last 3)
        prev_text = ""
        if prev_reflections:
            recent = prev_reflections[-3:] if len(prev_reflections) > 3 else prev_reflections
            prev_lines = []
            for i, r in enumerate(recent):
                try:
                    prev_lines.append(
                        f"{i + 1}. Decision was {r.decision}: {r.rationale}. "
                        f"Learned: {r.learning}"
                    )
                except AttributeError:
                    prev_lines.append(f"{i + 1}. (prior reflection)")
            prev_text = "\n".join(prev_lines) if prev_lines else "(none)"

        return (
            f"You just made a trading decision. Reflect on it.\n\n"
            f"## Decision\n"
            f"Ticker: {tick.ticker}\n"
            f"Action: {decision.decision}\n"
            f"Conviction: {decision.conviction:.2f}\n"
            f"Rationale: {decision.rationale}\n\n"
            f"## Market Context\n"
            f"{signal_text}\n\n"
            f"## Prior Reflections\n"
            f"{prev_text}\n\n"
            f"In 1-2 sentences each, answer:\n"
            f'1. What did you learn from this decision? ("learning")\n'
            f'2. What would you do differently next time? ("would_do_differently")\n\n'
            f'Respond with JSON: {{\"learning\": \"...\", "would_do_differently": "..."}}'
        )

    def _parse_reflection_response(self, content: Optional[str]) -> tuple[str, str]:
        """Parse LLM reflection response into (learning, would_do_differently)."""
        if not content:
            return (
                "API call failed — no learning recorded",
                "Check API connectivity",
            )

        # Strip markdown fences
        content = re.sub(r'^```(?:json)?\s*', '', content.strip())
        content = re.sub(r'\s*```$', '', content)

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r'\{[^{}]*"learning"[^{}]*\}', content)
            if match:
                try:
                    data = json.loads(match.group())
                except json.JSONDecodeError:
                    return (
                        f"Unparseable reflection: {content[:100]}",
                        "Improve response format",
                    )
            else:
                # If no JSON at all, treat the raw text as the learning
                text = content.strip()[:300]
                return (text, "N/A — no structured response")

        learning = str(data.get("learning", "No learning extracted"))[:300]
        would_do = str(data.get("would_do_differently", "No changes suggested"))[:300]
        return (learning, would_do)

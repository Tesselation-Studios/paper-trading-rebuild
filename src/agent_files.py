"""AgentFiles dataclass — shared context for all agent prompt assembly.

Broken out of llm_engine.py to avoid circular imports between
prompt_builder.py and llm_engine.py after PromptBuilder delegation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class AgentFiles:
    """Agent context files loaded at simulation start."""
    identity: str = ""    # IDENTITY.md
    agents_md: str = ""   # AGENTS.md — the operating manual
    soul: str = ""        # SOUL.md — personality
    tools: str = ""       # TOOLS.md — local setup
    memory: str = ""      # MEMORY.md — persistent learnings
    skills: List[str] = None  # skill names with 1-line summaries

    def __post_init__(self):
        if self.skills is None:
            self.skills = []

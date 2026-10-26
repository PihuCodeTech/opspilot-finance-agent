"""What the graph needs from outside, injected so tests can replace any part of it.

`policy_search` is hybrid retrieval in production and full-text in tests (no
model download). `llm` is the one LLM client (replay by default: zero cost).
"""

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from sqlalchemy import Engine

from opspilot.extraction.preprocess import Preprocessor
from opspilot.llm.client import LlmClient

PROMPTS = Path(__file__).with_name("prompts")
MAX_LLM_CALLS = 5  # per run (`05-stack.md` §4 quota plan)
RESERVED_FOR_EXPLAIN = 1  # the explanation always gets a call; optional steps yield first


@dataclass(frozen=True)
class PassageRef:
    id: uuid.UUID  # policy_chunks.id
    section_id: str
    title: str
    text: str


PolicySearch = Callable[[str], list[PassageRef]]


@dataclass(frozen=True)
class AgentDeps:
    engine: Engine
    llm: LlmClient
    policy_search: PolicySearch
    preprocessor: Preprocessor | None  # None: parsed documents must already exist (tests)
    document_store: Path
    max_llm_calls: int = MAX_LLM_CALLS


@cache
def prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


def prompt_versions() -> dict[str, str]:
    """Recorded on every run, so results can be traced to the exact prompt text."""
    return {
        p.stem: hashlib.sha256(p.read_bytes()).hexdigest()[:12]
        for p in sorted(PROMPTS.glob("*.md"))
    }


def budget_left(calls_used: int, deps: AgentDeps, *, optional: bool) -> int:
    """Calls still available; optional steps leave one for the explanation."""
    reserve = RESERVED_FOR_EXPLAIN if optional else 0
    return max(deps.max_llm_calls - calls_used - reserve, 0)

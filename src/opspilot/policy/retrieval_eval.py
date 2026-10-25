"""Measure retrieval on the labelled question set: full-text vs vector vs hybrid (D5)."""

import tomllib
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine

from opspilot.policy.retrieve import Embedder, retrieve

QUESTIONS = Path(__file__).with_name("retrieval_questions.toml")
MODES = ("fts", "vector", "hybrid")


@dataclass(frozen=True)
class ModeScore:
    mode: str
    recall_at_5: float  # mean share of expected sections found in the top 5
    mrr: float  # mean reciprocal rank of the first expected section (0 when absent)
    hit_rate: float  # questions with at least one expected section in the top 5
    no_answer_correct: float  # unrelated questions that returned nothing
    answerable: int
    unanswerable: int


def load_questions() -> list[tuple[str, list[str]]]:
    data = tomllib.loads(QUESTIONS.read_text(encoding="utf-8"))
    return [(q["q"], list(q["expected"])) for q in data["question"]]


def evaluate(engine: Engine, embedder: Embedder) -> list[ModeScore]:
    questions = load_questions()
    answerable = [(q, e) for q, e in questions if e]
    unanswerable = [q for q, e in questions if not e]
    scores = []
    for mode in MODES:
        recalls: list[float] = []
        reciprocal: list[float] = []
        hits = 0
        for question, expected in answerable:
            ranked = [p.section_id for p in retrieve(engine, embedder, question, mode=mode)]
            share = len(set(ranked) & set(expected)) / len(expected)
            recalls.append(share)
            hits += share > 0
            first = next((i for i, sid in enumerate(ranked, start=1) if sid in expected), None)
            reciprocal.append(1 / first if first else 0.0)
        empty = sum(not retrieve(engine, embedder, q, mode=mode) for q in unanswerable)
        scores.append(
            ModeScore(
                mode=mode,
                recall_at_5=sum(recalls) / len(recalls),
                mrr=sum(reciprocal) / len(reciprocal),
                hit_rate=hits / len(answerable),
                no_answer_correct=empty / len(unanswerable),
                answerable=len(answerable),
                unanswerable=len(unanswerable),
            )
        )
    return scores

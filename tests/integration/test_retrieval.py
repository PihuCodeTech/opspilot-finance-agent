"""Hybrid policy retrieval on a freshly seeded policy (real embeddings, real Postgres)."""

from functools import cache

import pytest
from sqlalchemy import Engine

from opspilot.db.seed import seed
from opspilot.policy.loader import load_policy
from opspilot.policy.retrieval_eval import evaluate
from opspilot.policy.retrieve import Embedder, active_version_id, index_policy, retrieve

pytestmark = [pytest.mark.integration, pytest.mark.models]


@cache
def embedder() -> Embedder:
    return Embedder()


@pytest.fixture
def indexed(owner_engine: Engine) -> Engine:
    seed(owner_engine, load_policy(), demo_password="local-demo-password")
    index_policy(owner_engine, embedder(), active_version_id(owner_engine))
    return owner_engine


@pytest.mark.parametrize(
    ("question", "section"),
    [
        ("callback to master contact", "7"),  # exact tokens
        ("The supplier says their bank account has changed", "7"),  # paraphrase
        ("Level 3 controller approval on the purchase order", "5"),
        ("How much above the purchase order price may a unit price be?", "3.1"),
    ],
)
def test_the_answering_section_ranks_first(indexed: Engine, question: str, section: str) -> None:
    passages = retrieve(indexed, embedder(), question)
    assert passages and passages[0].section_id == section
    assert len(passages) <= 5


def test_an_unrelated_question_gets_no_passage(indexed: Engine) -> None:
    assert retrieve(indexed, embedder(), "What is the office wifi password?") == []


def test_ranking_is_deterministic(indexed: Engine) -> None:
    question = "What counts as a duplicate invoice?"
    first = [p.section_id for p in retrieve(indexed, embedder(), question)]
    assert first == [p.section_id for p in retrieve(indexed, embedder(), question)]


def test_indexing_is_idempotent(indexed: Engine) -> None:
    assert index_policy(indexed, embedder(), active_version_id(indexed)) == 0


def test_measured_quality_holds(indexed: Engine) -> None:
    scores = {s.mode: s for s in evaluate(indexed, embedder())}
    assert scores["hybrid"].recall_at_5 >= 0.95
    assert scores["hybrid"].no_answer_correct == 1.0
    assert scores["hybrid"].mrr >= scores["fts"].mrr

"""Hybrid policy retrieval (D5, `06-architecture.md` §11).

Full-text search (Postgres, any-term match, `ts_rank_cd`) and vector search
(local `bge-small-en-v1.5`, cosine) each rank the policy sections; reciprocal
rank fusion merges them. A question-level gate comes first: if no section is at
least `ANSWERABLE_SIMILARITY` similar to the question, the answer is "no
applicable passage" rather than the least-bad section.

The gate was calibrated on the retrieval question set: unrelated questions
peaked at 0.56 and policy questions started at 0.62 (`retrieval_questions.toml`).
"""

import uuid
from dataclasses import dataclass
from functools import cached_property
from typing import Any

from sqlalchemy import Engine, select, text, update
from sqlalchemy.orm import Session

from opspilot.db.models import EMBEDDING_DIMENSIONS, PolicyChunk, PolicyVersion

EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
# bge models expect this instruction on queries (not on passages).
QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "
RRF_K = 60
CANDIDATES = 8
TOP = 5
ANSWERABLE_SIMILARITY = 0.60


class Embedder:
    """Loads the local embedding model once per process; CPU only, nothing leaves the machine."""

    @cached_property
    def _model(self) -> Any:
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415  # heavy import

        return SentenceTransformer(EMBEDDING_MODEL, device="cpu")

    def passages(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(
            texts, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False
        )
        return [[float(x) for x in v] for v in vectors]

    def query(self, question: str) -> list[float]:
        return self.passages([QUERY_INSTRUCTION + question])[0]


@dataclass(frozen=True)
class Passage:
    section_id: str
    title: str
    text: str
    chunk_id: uuid.UUID
    fts_rank: int | None
    vector_rank: int | None
    similarity: float
    score: float


def active_version_id(engine: Engine) -> uuid.UUID:
    with Session(engine) as session:
        version_id = session.scalar(select(PolicyVersion.id).where(PolicyVersion.is_active))
    if version_id is None:
        raise RuntimeError("no active policy version; run `opspilot seed`")
    return version_id


def index_policy(engine: Engine, embedder: Embedder, version_id: uuid.UUID) -> int:
    """Embed every chunk of a policy version that has no embedding yet (owner role)."""
    with Session(engine) as session, session.begin():
        chunks = session.execute(
            select(PolicyChunk.id, PolicyChunk.title, PolicyChunk.text).where(
                PolicyChunk.policy_version_id == version_id, PolicyChunk.embedding.is_(None)
            )
        ).all()
        if not chunks:
            return 0
        vectors = embedder.passages([f"{c.title}\n{c.text}" for c in chunks])
        for chunk, vector in zip(chunks, vectors, strict=True):
            if len(vector) != EMBEDDING_DIMENSIONS:
                raise RuntimeError(
                    f"embedding has {len(vector)} dimensions, expected {EMBEDDING_DIMENSIONS}"
                )
            session.execute(
                update(PolicyChunk).where(PolicyChunk.id == chunk.id).values(embedding=vector)
            )
    return len(chunks)


def _any_term_query(engine: Engine, question: str) -> str | None:
    """Stemmed query lexemes joined with OR, quoted for to_tsquery('simple', …)."""
    with engine.connect() as conn:
        lexemes = conn.execute(
            text("SELECT tsvector_to_array(to_tsvector('english', :q))"), {"q": question}
        ).scalar_one()
    if not lexemes:
        return None
    return " | ".join("'" + lexeme.replace("'", "''") + "'" for lexeme in lexemes)


def _full_text(engine: Engine, version_id: uuid.UUID, question: str) -> list[uuid.UUID]:
    tsquery = _any_term_query(engine, question)
    if tsquery is None:
        return []
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id FROM policy_chunks WHERE policy_version_id = :v"
                " AND tsv @@ to_tsquery('simple', :q)"
                " ORDER BY ts_rank_cd(tsv, to_tsquery('simple', :q)) DESC, section_id LIMIT :n"
            ),
            {"v": version_id, "q": tsquery, "n": CANDIDATES},
        ).scalars()
        return list(rows)


def _vectors(
    engine: Engine, version_id: uuid.UUID, query_vector: list[float]
) -> list[tuple[uuid.UUID, float]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, 1 - (embedding <=> CAST(:q AS vector)) AS similarity FROM policy_chunks"
                " WHERE policy_version_id = :v AND embedding IS NOT NULL"
                " ORDER BY embedding <=> CAST(:q AS vector), section_id LIMIT :n"
            ),
            {"v": version_id, "q": str(query_vector), "n": CANDIDATES},
        ).all()
    return [(row.id, float(row.similarity)) for row in rows]


def retrieve(
    engine: Engine,
    embedder: Embedder,
    question: str,
    *,
    version_id: uuid.UUID | None = None,
    mode: str = "hybrid",  # hybrid | fts | vector (the latter two for the retrieval evaluation)
    top: int = TOP,
) -> list[Passage]:
    version_id = version_id or active_version_id(engine)
    vec = _vectors(engine, version_id, embedder.query(question)) if mode != "fts" else []
    if mode != "fts" and (not vec or vec[0][1] < ANSWERABLE_SIMILARITY):
        return []  # nothing in the policy is about this question
    fts = _full_text(engine, version_id, question) if mode != "vector" else []
    fts_rank = {chunk_id: rank for rank, chunk_id in enumerate(fts, start=1)}
    vec_rank = {chunk_id: rank for rank, (chunk_id, _) in enumerate(vec, start=1)}
    similarity = dict(vec)

    candidates = set(fts_rank) | set(vec_rank)
    scored = sorted(
        (
            (sum(1 / (RRF_K + r[cid]) for r in (fts_rank, vec_rank) if cid in r), cid)
            for cid in candidates
        ),
        key=lambda pair: (-pair[0], str(pair[1])),
    )[:top]
    if not scored:
        return []
    with Session(engine) as session:
        chunks = {
            c.id: c
            for c in session.scalars(
                select(PolicyChunk).where(PolicyChunk.id.in_([c for _, c in scored]))
            )
        }
    return [
        Passage(
            chunks[cid].section_id,
            chunks[cid].title,
            chunks[cid].text,
            cid,
            fts_rank.get(cid),
            vec_rank.get(cid),
            similarity.get(cid, 0.0),
            score,
        )
        for score, cid in scored
    ]

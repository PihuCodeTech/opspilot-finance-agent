"""Deterministic invoice-line → PO-line matching (`06-architecture.md` §3.1).

In order: exact SKU, exact normalised description, then text similarity above
a threshold, where the best candidate must also clearly beat the runner-up.
Each PO line is used at most once. Lines that remain unmatched (and
ambiguous ones) go to the LLM matching step (P10), whose proposal is
validated; they are never guessed here.
"""

import re
from dataclasses import dataclass
from decimal import Decimal
from difflib import SequenceMatcher

from opspilot.domain.enums import MatchMethod
from opspilot.truthlens.types import InvoiceLineFacts, PoLineRecord

SIMILARITY_THRESHOLD = Decimal("0.80")
CLEAR_MARGIN = Decimal("0.10")
_WORD = re.compile(r"[a-z0-9]+")
_SYNONYMS = {"pcs": "", "ea": "", "each": "", "x": "", "mm": "", "the": "", "of": ""}


@dataclass(frozen=True)
class LineMatch:
    invoice_line: int
    po_line_no: int | None
    method: MatchMethod
    similarity: Decimal | None


def normalise_description(text: str) -> str:
    words = [_SYNONYMS.get(w, w) for w in _WORD.findall(text.lower())]
    return " ".join(w for w in words if w)


def similarity(a: str, b: str) -> Decimal:
    na, nb = normalise_description(a), normalise_description(b)
    sequence = SequenceMatcher(None, na, nb).ratio()
    tokens_a, tokens_b = set(na.split()), set(nb.split())
    jaccard = len(tokens_a & tokens_b) / len(tokens_a | tokens_b) if tokens_a | tokens_b else 0.0
    return Decimal(str(round(max(sequence, jaccard), 3)))


def _norm_sku(sku: str | None) -> str | None:
    return re.sub(r"[^a-z0-9]", "", sku.lower()) if sku else None


def match_lines(
    lines: tuple[InvoiceLineFacts, ...], po_lines: tuple[PoLineRecord, ...]
) -> list[LineMatch]:
    available = {pl.line_no: pl for pl in po_lines}
    result: dict[int, LineMatch] = {}

    for line in lines:  # 1. SKU
        sku = _norm_sku(line.sku)
        hit = next((pl for pl in available.values() if sku and _norm_sku(pl.sku) == sku), None)
        if hit is not None:
            result[line.index] = LineMatch(line.index, hit.line_no, MatchMethod.SKU, Decimal(1))
            del available[hit.line_no]

    for line in lines:  # 2. exact normalised description
        if line.index in result:
            continue
        text = normalise_description(line.description)
        hit = next(
            (pl for pl in available.values() if normalise_description(pl.description) == text), None
        )
        if hit is not None:
            result[line.index] = LineMatch(
                line.index, hit.line_no, MatchMethod.DESCRIPTION, Decimal(1)
            )
            del available[hit.line_no]

    for line in lines:  # 3. clear similarity winner
        if line.index in result:
            continue
        scored = sorted(
            (
                (similarity(line.description, pl.description), pl.line_no)
                for pl in available.values()
            ),
            reverse=True,
        )
        if (
            scored
            and scored[0][0] >= SIMILARITY_THRESHOLD
            and (len(scored) == 1 or scored[0][0] - scored[1][0] >= CLEAR_MARGIN)
        ):
            score, line_no = scored[0]
            result[line.index] = LineMatch(line.index, line_no, MatchMethod.DESCRIPTION, score)
            del available[line_no]
        else:
            best = scored[0][0] if scored else None
            result[line.index] = LineMatch(line.index, None, MatchMethod.UNMATCHED, best)

    return [result[line.index] for line in lines]

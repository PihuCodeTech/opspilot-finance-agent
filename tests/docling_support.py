"""Shared Docling test helpers: one converter per test session (loading models takes seconds)."""

import json
from functools import cache
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "documents"
KEY = "test-key-for-preprocess-0000000000000000"


def require_docling() -> None:
    try:
        import docling  # noqa: F401, PLC0415
    except ImportError:  # pragma: no cover
        pytest.skip("Docling is not installed")


@cache
def preprocessor() -> Any:
    from opspilot.extraction.preprocess import Preprocessor  # noqa: PLC0415

    return Preprocessor(KEY)


def truth(name: str) -> dict[str, str]:
    data: dict[str, str] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return data

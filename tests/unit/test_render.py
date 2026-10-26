"""Real PDF rendering; runs where WeasyPrint's system libraries exist (Docker, CI).

Mandatory in CI (`CI` or `OPSPILOT_REQUIRE_RENDER` set): a missing library fails
the run there instead of silently skipping. Only a developer machine may skip.
"""

import os

import pytest

from opspilot.evaluation.generator.build import DEFAULT_SPECS, generate
from opspilot.evaluation.generator.model import Item
from opspilot.evaluation.generator.render import invoice_html, render_pdf
from opspilot.policy.loader import load_policy

try:
    import weasyprint  # type: ignore[import-untyped]  # noqa: F401
except OSError:  # Pango missing on this machine
    if os.environ.get("CI") or os.environ.get("OPSPILOT_REQUIRE_RENDER"):
        raise  # required here: never skip in CI
    pytest.skip("WeasyPrint system libraries are not installed", allow_module_level=True)


def test_every_layout_renders_a_deterministic_pdf() -> None:
    world = generate(DEFAULT_SPECS["final"], load_policy().config)
    by_layout: dict[str, Item] = {}
    for item in world.items:
        by_layout.setdefault(item.invoice.layout, item)
    assert len(by_layout) == 6
    for item in by_layout.values():
        html = invoice_html(item, world)
        first, second = render_pdf(html), render_pdf(html)
        assert first.startswith(b"%PDF")
        assert first == second

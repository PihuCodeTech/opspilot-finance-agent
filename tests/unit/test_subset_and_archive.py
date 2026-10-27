from collections import Counter
from pathlib import Path

from tests.unit.test_bundle_and_bank import fake_pdf

from opspilot.evaluation.archive import archive_bundle
from opspilot.evaluation.generator.build import DEFAULT_SPECS, SplitSpec, generate
from opspilot.evaluation.generator.bundle import labels_document, write_bundle
from opspilot.evaluation.subset import stratified_subset, stratum
from opspilot.policy.loader import load_policy

POLICY = load_policy().config


def test_subset_is_fixed_stratified_and_sized() -> None:
    labels = labels_document(generate(DEFAULT_SPECS["final"], POLICY))
    subset = stratified_subset(labels)
    assert subset == stratified_subset(labels)  # never re-drawn differently
    assert len(subset) == 150 and len(set(subset)) == 150
    full = Counter(stratum(v) for v in labels.values())
    drawn = Counter(stratum(labels[k]) for k in subset)
    for name, count in full.items():  # each stratum within one item of its proportional share
        assert abs(drawn[name] - count * 150 / 600) <= 1, name


def test_archive_is_byte_reproducible(tmp_path: Path) -> None:
    world = generate(SplitSpec("demo", 4004, 6, 0.5), POLICY)
    bundle = write_bundle(world, tmp_path / "b", renderer=fake_pdf)
    _, first = archive_bundle(bundle, tmp_path / "r1")
    _, second = archive_bundle(bundle, tmp_path / "r2")
    assert first == second

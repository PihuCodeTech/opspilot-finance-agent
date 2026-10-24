"""Loading and freezing dataset bundles, as the scorer role."""

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text

from opspilot.domain.bank import fingerprint
from opspilot.evaluation.datasets import DatasetError, freeze_dataset, load_bundle
from opspilot.evaluation.generator.build import SplitSpec, generate
from opspilot.evaluation.generator.bundle import write_bundle
from opspilot.policy.loader import load_policy
from tests.unit.test_bundle_and_bank import fake_pdf

pytestmark = pytest.mark.integration
KEY = "test-bank-hmac-key-0000000000000000000000"


def seed_of(version: str) -> int:
    return int(version.split("-s")[1])


@pytest.fixture
def scorer_engine(migrated_url: str) -> Iterator[Engine]:
    engine = create_engine(migrated_url, connect_args={"options": "-c role=opspilot_scorer"})
    yield engine
    engine.dispose()


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    seed = uuid.uuid4().int % 10_000 + 10_000  # unique per test: datasets persist
    world = generate(SplitSpec("demo", seed, 12, 0.5), load_policy().config)
    return write_bundle(world, tmp_path / "bundles", renderer=fake_pdf)


def test_load_as_scorer_then_freeze(
    scorer_engine: Engine, owner_engine: Engine, bundle: Path, tmp_path: Path
) -> None:
    store = tmp_path / "documents"
    result = load_bundle(scorer_engine, bundle, store, KEY)
    assert result.items == 12
    assert len(list(store.rglob("*.pdf"))) == 12

    with owner_engine.connect() as conn:
        items = conn.execute(
            text("SELECT count(*) FROM dataset_items WHERE dataset_id = :d"),
            {"d": result.dataset_id},
        ).scalar_one()
        fps = (
            conn.execute(
                text(
                    "SELECT b.bank_fp FROM vendor_bank_accounts b"
                    " JOIN vendors v ON v.id = b.vendor_id WHERE v.dataset_id = :d"
                ),
                {"d": result.dataset_id},
            )
            .scalars()
            .all()
        )
        dump = conn.execute(
            text("SELECT string_agg(row_to_json(v)::text, '') FROM vendor_bank_accounts v")
        ).scalar_one()
    assert items == 12
    assert all(len(fp) == 12 for fp in fps)
    world_numbers = [
        a.number
        for v in generate(
            SplitSpec("demo", seed_of(result.version), 12, 0.5), load_policy().config
        ).vendors
        for a in v.accounts
    ]
    assert fingerprint(world_numbers[0], KEY).bank_fp in fps
    for number in world_numbers:  # no full account number reaches the database
        assert number.replace(" ", "") not in dump

    with pytest.raises(DatasetError):
        load_bundle(scorer_engine, bundle, store, KEY)  # loading twice is refused

    freeze_dataset(scorer_engine, bundle)
    with owner_engine.connect() as conn, pytest.raises(Exception, match="frozen"):
        conn.execute(
            text("UPDATE vendors SET is_approved = NOT is_approved WHERE dataset_id = :d"),
            {"d": result.dataset_id},
        )

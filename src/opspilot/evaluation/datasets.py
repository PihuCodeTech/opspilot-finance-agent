"""Load dataset bundles into the database and freeze them (`06-database.md` §9).

Runs as the scorer role: the only runtime role allowed to write datasets and
read labels. Bank account numbers from the bundle are reduced to fingerprints
here; no full number reaches the database.
"""

import datetime as dt
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from opspilot.db.models import (
    Dataset,
    DatasetItem,
    Document,
    Invoice,
    LedgerInvoice,
    LedgerInvoiceLine,
    PoLine,
    PurchaseOrder,
    Receipt,
    ReceiptLine,
    Vendor,
    VendorBankAccount,
)
from opspilot.domain.bank import fingerprint
from opspilot.domain.enums import DatasetSplit, DocumentKind, DocumentSource
from opspilot.evaluation.generator.bundle import (
    BundleError,
    labels_document,
    manifest_sha256,
    read_bundle,
    read_manifest,
)


class DatasetError(RuntimeError):
    pass


@dataclass(frozen=True)
class LoadResult:
    dataset_id: uuid.UUID
    name: str
    version: str
    items: int


def _store_document(source: Path, sha256: str, store: Path) -> str:
    relative = Path(sha256[:2]) / f"{sha256}.pdf"
    target = store / relative
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return str(relative)


def load_bundle(engine: Engine, bundle: Path, document_store: Path, bank_key: str) -> LoadResult:
    manifest = read_manifest(bundle)
    world = read_bundle(bundle)  # verifies every file against the manifest first
    labels = labels_document(world)
    with Session(engine) as session, session.begin():
        exists = session.scalar(
            select(Dataset.id).where(
                Dataset.name == manifest["name"], Dataset.version == manifest["version"]
            )
        )
        if exists is not None:
            raise DatasetError(
                f"dataset {manifest['name']} {manifest['version']} is already loaded"
            )
        dataset = Dataset(
            name=manifest["name"],
            version=manifest["version"],
            split=DatasetSplit(manifest["split"]),
            seed=manifest["seed"],
            generator_version=manifest["generator_version"],
            manifest_sha256=manifest_sha256(bundle),
        )
        session.add(dataset)
        session.flush()

        vendor_ids: dict[str, uuid.UUID] = {}
        for v in world.vendors:
            row = Vendor(
                dataset_id=dataset.id,
                vendor_code=v.code,
                name=v.name,
                address=v.address,
                tax_rate=v.tax_rate,
                is_approved=v.approved,
            )
            session.add(row)
            session.flush()
            vendor_ids[v.code] = row.id
            for account in v.accounts:
                fp = fingerprint(account.number, bank_key)
                session.add(
                    VendorBankAccount(
                        vendor_id=row.id,
                        bank_fp=fp.bank_fp,
                        last4=fp.last4,
                        valid_from=account.valid_from,
                        valid_to=account.valid_to,
                    )
                )

        po_ids: dict[str, uuid.UUID] = {}
        po_line_ids: dict[tuple[str, int], uuid.UUID] = {}
        for p in world.purchase_orders:
            po = PurchaseOrder(
                dataset_id=dataset.id,
                po_number=p.number,
                vendor_id=vendor_ids[p.vendor_code],
                approval_level=p.approval_level,
                controller_approved=p.controller_approved,
                issued_at=p.issued_at,
            )
            session.add(po)
            session.flush()
            po_ids[p.number] = po.id
            for pln in p.lines:
                line = PoLine(
                    po_id=po.id,
                    line_no=pln.line_no,
                    sku=pln.sku,
                    description=pln.description,
                    quantity=pln.quantity,
                    unit_price=pln.unit_price,
                )
                session.add(line)
                session.flush()
                po_line_ids[(p.number, pln.line_no)] = line.id

        for r in world.receipts:
            receipt = Receipt(
                dataset_id=dataset.id,
                receipt_number=r.number,
                po_id=po_ids[r.po_number],
                received_at=r.received_at,
            )
            session.add(receipt)
            session.flush()
            for rl in r.lines:
                session.add(
                    ReceiptLine(
                        receipt_id=receipt.id,
                        po_line_id=po_line_ids[(r.po_number, rl.po_line_no)],
                        quantity_received=rl.quantity,
                    )
                )

        for led in world.ledger:
            ledger = LedgerInvoice(
                dataset_id=dataset.id,
                vendor_id=vendor_ids[led.vendor_code],
                po_id=po_ids.get(led.po_number or ""),
                invoice_number=led.number,
                invoice_date=led.invoice_date,
                subtotal=led.subtotal,
                tax=led.tax,
                total=led.total,
            )
            session.add(ledger)
            session.flush()
            for ln in led.lines:
                po_line = po_line_ids.get((led.po_number or "", ln.po_line_no or 0))
                session.add(
                    LedgerInvoiceLine(
                        ledger_invoice_id=ledger.id,
                        po_line_id=po_line,
                        sku=ln.sku,
                        description=ln.description,
                        quantity=ln.quantity,
                        unit_price=ln.unit_price,
                        line_total=ln.line_total,
                    )
                )

        files = manifest["files"]
        for item in world.items:
            relative = f"documents/{item.invoice.key}.pdf"
            sha = files[relative]
            source = bundle / relative
            document = Document(
                sha256=sha,
                kind=DocumentKind.INVOICE,
                mime_type="application/pdf",
                size_bytes=source.stat().st_size,
                page_count=1,
                storage_path=_store_document(source, sha, document_store),
                source=DocumentSource.GENERATED,
            )
            session.add(document)
            session.flush()
            invoice = Invoice(
                dataset_id=dataset.id, document_id=document.id, received_at=item.invoice.received_at
            )
            session.add(invoice)
            session.flush()
            session.add(
                DatasetItem(
                    dataset_id=dataset.id,
                    invoice_id=invoice.id,
                    labels={"key": item.invoice.key, **labels[item.invoice.key]},
                )
            )
        return LoadResult(dataset.id, dataset.name, dataset.version, len(world.items))


def freeze_dataset(engine: Engine, bundle: Path) -> dt.datetime:
    """Freeze a loaded dataset; its ERP rows and items become immutable (trigger-enforced)."""
    manifest = read_manifest(bundle)
    expected = manifest_sha256(bundle)
    with Session(engine) as session, session.begin():
        dataset = session.scalar(
            select(Dataset).where(
                Dataset.name == manifest["name"], Dataset.version == manifest["version"]
            )
        )
        if dataset is None:
            raise DatasetError("dataset is not loaded")
        if dataset.manifest_sha256 != expected:
            raise BundleError("the loaded dataset was built from a different manifest")
        if dataset.frozen_at is not None:
            return dataset.frozen_at
        now = dt.datetime.now(dt.UTC)
        session.execute(update(Dataset).where(Dataset.id == dataset.id).values(frozen_at=now))
        return now

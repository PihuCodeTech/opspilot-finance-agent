"""Dataset bundles on disk: world data, labels, PDFs and a checksummed manifest.

    <bundle>/world.json      vendors, POs, receipts, ledger, incoming invoices (no labels)
    <bundle>/labels.json     per-item labels; read only by the loader and the scorer
    <bundle>/documents/*.pdf one PDF per incoming invoice
    <bundle>/manifest.json   names, seed, generator version, SHA-256 of every file

Labels live in their own file so an inference process never needs to mount
them (`rules.md` §2). The frozen final set ships as one archive of a bundle.
"""

import dataclasses
import datetime as dt
import hashlib
import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

from opspilot.domain.enums import ActionType
from opspilot.evaluation.generator.model import (
    BankAccount,
    IncomingInvoice,
    InvoiceLine,
    Item,
    Labels,
    LedgerInvoice,
    PoLine,
    PurchaseOrder,
    Receipt,
    ReceiptLine,
    Vendor,
    World,
)
from opspilot.evaluation.generator.render import invoice_html, render_pdf

MANIFEST = "manifest.json"
WORLD = "world.json"
LABELS = "labels.json"
DOCUMENTS = "documents"


class BundleError(RuntimeError):
    pass


def bundle_name(world: World) -> str:
    return f"{world.split}-g{world.generator_version}-s{world.seed}"


def _jsonable(value: Any) -> Any:  # noqa: PLR0911  # one return per supported type
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, ActionType):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def _dumps(value: Any) -> bytes:
    return (
        json.dumps(_jsonable(value), indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode()


def world_document(world: World) -> dict[str, Any]:
    data: dict[str, Any] = _jsonable(dataclasses.replace(world, items=[]))
    data["invoices"] = [_jsonable(item.invoice) for item in world.items]
    del data["items"]
    return data


def labels_document(world: World) -> dict[str, Any]:
    return {item.invoice.key: _jsonable(item.labels) for item in world.items}


def _d(value: str) -> Decimal:
    return Decimal(value)


def _date(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def _line(d: dict[str, Any]) -> InvoiceLine:
    return InvoiceLine(
        d["description"],
        d["sku"],
        _d(d["quantity"]),
        _d(d["unit_price"]),
        _d(d["line_total"]),
        d["po_line_no"],
    )


def world_from_documents(world_doc: dict[str, Any], labels_doc: dict[str, Any]) -> World:
    world = World(world_doc["split"], world_doc["seed"], world_doc["generator_version"])
    for v in world_doc["vendors"]:
        world.vendors.append(
            Vendor(
                v["code"],
                v["name"],
                v["address"],
                _d(v["tax_rate"]),
                v["approved"],
                [
                    BankAccount(
                        a["number"],
                        _date(a["valid_from"]),
                        _date(a["valid_to"]) if a["valid_to"] else None,
                    )
                    for a in v["accounts"]
                ],
                v["contact_phone"],
            )
        )
    for p in world_doc["purchase_orders"]:
        world.purchase_orders.append(
            PurchaseOrder(
                p["number"],
                p["vendor_code"],
                p["approval_level"],
                p["controller_approved"],
                _date(p["issued_at"]),
                [
                    PoLine(
                        ln["line_no"],
                        ln["sku"],
                        ln["description"],
                        _d(ln["quantity"]),
                        _d(ln["unit_price"]),
                    )
                    for ln in p["lines"]
                ],
            )
        )
    for r in world_doc["receipts"]:
        world.receipts.append(
            Receipt(
                r["number"],
                r["po_number"],
                _date(r["received_at"]),
                [ReceiptLine(ln["po_line_no"], _d(ln["quantity"])) for ln in r["lines"]],
            )
        )
    for led in world_doc["ledger"]:
        world.ledger.append(
            LedgerInvoice(
                led["number"],
                led["vendor_code"],
                led["po_number"],
                _date(led["invoice_date"]),
                [_line(ln) for ln in led["lines"]],
                _d(led["subtotal"]),
                _d(led["tax"]),
                _d(led["total"]),
            )
        )
    for inv in world_doc["invoices"]:
        labels = labels_doc[inv["key"]]
        world.items.append(
            Item(
                IncomingInvoice(
                    inv["key"],
                    inv["number"],
                    inv["vendor_code"],
                    inv["po_number"],
                    _date(inv["invoice_date"]),
                    dt.datetime.fromisoformat(inv["received_at"]),
                    [_line(ln) for ln in inv["lines"]],
                    _d(inv["subtotal"]),
                    _d(inv["tax"]),
                    _d(inv["total"]),
                    inv["bank_account"],
                    inv["layout"],
                    inv["notes"],
                ),
                Labels(
                    labels["exceptions"],
                    [ActionType(a) for a in labels["acceptable_actions"]],
                    labels["is_clean"],
                    labels["is_adversarial"],
                    labels["variant_tags"],
                    labels["extraction_truth"],
                ),
            )
        )
    return world


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_bundle(
    world: World, root: Path, *, renderer: Callable[[str], bytes] = render_pdf
) -> Path:
    target = root / bundle_name(world)
    if target.exists():
        raise BundleError(f"{target} already exists; bundles are never overwritten")
    (target / DOCUMENTS).mkdir(parents=True)
    files: dict[str, str] = {}
    for name, data in (
        (WORLD, _dumps(world_document(world))),
        (LABELS, _dumps(labels_document(world))),
    ):
        (target / name).write_bytes(data)
        files[name] = _sha256(data)
    for item in world.items:
        pdf = renderer(invoice_html(item, world))
        relative = f"{DOCUMENTS}/{item.invoice.key}.pdf"
        (target / relative).write_bytes(pdf)
        files[relative] = _sha256(pdf)
    manifest = {
        "name": world.split,
        "version": f"g{world.generator_version}-s{world.seed}",
        "split": world.split,
        "seed": world.seed,
        "generator_version": world.generator_version,
        "item_count": len(world.items),
        "files": dict(sorted(files.items())),
    }
    (target / MANIFEST).write_bytes(_dumps(manifest))
    return target


def read_manifest(bundle: Path) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads((bundle / MANIFEST).read_text(encoding="utf-8"))
    return manifest


def manifest_sha256(bundle: Path) -> str:
    return _sha256((bundle / MANIFEST).read_bytes())


def verify_bundle(bundle: Path) -> list[str]:
    """Return the problems found; an empty list means every file matches the manifest."""
    problems = []
    manifest = read_manifest(bundle)
    for relative, expected in manifest["files"].items():
        path = bundle / relative
        if not path.exists():
            problems.append(f"missing: {relative}")
        elif _sha256(path.read_bytes()) != expected:
            problems.append(f"changed: {relative}")
    listed = set(manifest["files"])
    extra = {
        str(p.relative_to(bundle)) for p in bundle.rglob("*") if p.is_file() and p.name != MANIFEST
    } - listed
    problems.extend(f"unexpected: {name}" for name in sorted(extra))
    return problems


def read_bundle(bundle: Path) -> World:
    problems = verify_bundle(bundle)
    if problems:
        raise BundleError("bundle does not match its manifest: " + "; ".join(problems))
    world_doc = json.loads((bundle / WORLD).read_text(encoding="utf-8"))
    labels_doc = json.loads((bundle / LABELS).read_text(encoding="utf-8"))
    return world_from_documents(world_doc, labels_doc)

"""SQLAlchemy models: the schema of `06-database.md` §3.

These models are the single schema definition: the first migration is
generated from them, and a test fails if they and the migrations drift apart.
Roles, grants, views and integrity triggers live in raw-SQL migrations.
"""

import datetime as dt
import uuid
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from opspilot.domain.enums import (
    ActionType,
    ApproverTier,
    CaseStatus,
    CheckCode,
    CheckStatus,
    DatasetSplit,
    DecisionKind,
    DocumentKind,
    DocumentSource,
    EvalRunStatus,
    EvalSystem,
    FindingCode,
    GroundingStatus,
    JobStatus,
    JobType,
    MatchMethod,
    ProposalSource,
    ResolutionType,
    RunKind,
    RunStatus,
    Severity,
    SupportingKind,
    UserRole,
    VerificationChannel,
)

EMBEDDING_DIMENSIONS = 384  # bge-small-en-v1.5 (D5)

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def pg_enum(enum: type[StrEnum], name: str) -> SAEnum:
    return SAEnum(
        enum, name=name, values_callable=lambda e: [m.value for m in e], validate_strings=True
    )


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, server_default=sql_text("gen_random_uuid()"))


def now_column() -> Mapped[dt.datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


MONEY = Numeric(14, 2)
UNIT_PRICE = Numeric(14, 4)
QUANTITY = Numeric(12, 3)
SHA256_HEX = String(64)
BANK_FP = String(12)
LAST4 = String(4)

# ---------------------------------------------------------------- identity


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "(role <> 'approver' AND approval_limit IS NULL AND NOT approval_unlimited)"
            " OR (role = 'approver' AND (approval_limit IS NOT NULL) <> approval_unlimited)",
            name="approval_authority",
        ),
        CheckConstraint("approval_limit IS NULL OR approval_limit > 0", name="positive_limit"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    username: Mapped[str] = mapped_column(Text, unique=True)
    display_name: Mapped[str] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[UserRole] = mapped_column(pg_enum(UserRole, "user_role"))
    approval_limit: Mapped[Decimal | None] = mapped_column(MONEY)
    approval_unlimited: Mapped[bool] = mapped_column(Boolean, server_default=sql_text("false"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=sql_text("true"))
    created_at: Mapped[dt.datetime] = now_column()


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = uuid_pk()
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    csrf_token: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = now_column()
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------- simulated ERP


class Dataset(Base):
    __tablename__ = "datasets"
    __table_args__ = (UniqueConstraint("name", "version"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text)
    version: Mapped[str] = mapped_column(Text)
    split: Mapped[DatasetSplit] = mapped_column(pg_enum(DatasetSplit, "dataset_split"))
    seed: Mapped[int | None] = mapped_column(BigInteger)
    generator_version: Mapped[str | None] = mapped_column(Text)
    manifest_sha256: Mapped[str | None] = mapped_column(SHA256_HEX)
    frozen_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = now_column()


class Vendor(Base):
    __tablename__ = "vendors"
    __table_args__ = (
        UniqueConstraint("dataset_id", "vendor_code"),
        CheckConstraint("tax_rate >= 0 AND tax_rate < 1", name="tax_rate_range"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    dataset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("datasets.id"))
    vendor_code: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    address: Mapped[str] = mapped_column(Text)
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(5, 4))
    is_approved: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[dt.datetime] = now_column()


class VendorBankAccount(Base):
    """Only a short keyed fingerprint and the last 4 digits: no account number (§6)."""

    __tablename__ = "vendor_bank_accounts"
    __table_args__ = (
        Index(
            "uq_vendor_bank_accounts_current",
            "vendor_id",
            unique=True,
            postgresql_where=sql_text("valid_to IS NULL"),
        ),
        CheckConstraint("bank_fp ~ '^[0-9a-f]{12}$'", name="bank_fp_format"),
        CheckConstraint("last4 ~ '^[0-9]{4}$'", name="last4_format"),
        CheckConstraint("valid_to IS NULL OR valid_to >= valid_from", name="valid_range"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    vendor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vendors.id"))
    bank_fp: Mapped[str] = mapped_column(BANK_FP)
    last4: Mapped[str] = mapped_column(LAST4)
    valid_from: Mapped[dt.date] = mapped_column(Date)
    valid_to: Mapped[dt.date | None] = mapped_column(Date)


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (
        UniqueConstraint("dataset_id", "po_number"),
        CheckConstraint("currency = 'USD'", name="usd_only"),
        CheckConstraint("approval_level IN (1, 2, 3)", name="approval_level"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    dataset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("datasets.id"))
    po_number: Mapped[str] = mapped_column(Text)
    vendor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vendors.id"))
    approval_level: Mapped[int] = mapped_column(SmallInteger)
    controller_approved: Mapped[bool] = mapped_column(Boolean, server_default=sql_text("false"))
    currency: Mapped[str] = mapped_column(String(3), server_default=sql_text("'USD'"))
    issued_at: Mapped[dt.date] = mapped_column(Date)


class PoLine(Base):
    __tablename__ = "po_lines"
    __table_args__ = (
        UniqueConstraint("po_id", "line_no"),
        CheckConstraint("quantity > 0", name="positive_quantity"),
        CheckConstraint("unit_price >= 0", name="non_negative_price"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    po_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("purchase_orders.id"))
    line_no: Mapped[int] = mapped_column(Integer)
    sku: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    unit_price: Mapped[Decimal] = mapped_column(UNIT_PRICE)


class Receipt(Base):
    __tablename__ = "receipts"
    __table_args__ = (UniqueConstraint("dataset_id", "receipt_number"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    dataset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("datasets.id"))
    receipt_number: Mapped[str] = mapped_column(Text)
    po_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("purchase_orders.id"))
    received_at: Mapped[dt.date] = mapped_column(Date)


class ReceiptLine(Base):
    __tablename__ = "receipt_lines"
    __table_args__ = (CheckConstraint("quantity_received > 0", name="positive_quantity"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    receipt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("receipts.id"))
    po_line_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("po_lines.id"), index=True)
    quantity_received: Mapped[Decimal] = mapped_column(QUANTITY)


class LedgerInvoice(Base):
    """An invoice the ERP has already processed (duplicates, prior billing)."""

    __tablename__ = "ledger_invoices"
    __table_args__ = (
        Index("ix_ledger_invoices_number", "dataset_id", "vendor_id", "invoice_number_norm"),
        Index("ix_ledger_invoices_date", "dataset_id", "vendor_id", "invoice_date"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    dataset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("datasets.id"))
    vendor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vendors.id"))
    po_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("purchase_orders.id"))
    invoice_number: Mapped[str] = mapped_column(Text)
    invoice_number_norm: Mapped[str] = mapped_column(
        Text,
        Computed("regexp_replace(lower(invoice_number), '[^a-z0-9]', '', 'g')", persisted=True),
    )
    invoice_date: Mapped[dt.date] = mapped_column(Date)
    subtotal: Mapped[Decimal] = mapped_column(MONEY)
    tax: Mapped[Decimal] = mapped_column(MONEY)
    total: Mapped[Decimal] = mapped_column(MONEY)


class LedgerInvoiceLine(Base):
    __tablename__ = "ledger_invoice_lines"

    id: Mapped[uuid.UUID] = uuid_pk()
    ledger_invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ledger_invoices.id"))
    po_line_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("po_lines.id"), index=True)
    # As printed on the processed invoice: the duplicate check compares line sets by SKU,
    # or by description where no SKU was printed (docs/datasets.md).
    sku: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    unit_price: Mapped[Decimal] = mapped_column(UNIT_PRICE)
    line_total: Mapped[Decimal] = mapped_column(MONEY)


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            "mime_type IN ('application/pdf', 'image/png', 'image/jpeg')", name="mime_type"
        ),
        CheckConstraint("size_bytes > 0", name="positive_size"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_format"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    sha256: Mapped[str] = mapped_column(SHA256_HEX, unique=True)
    kind: Mapped[DocumentKind] = mapped_column(pg_enum(DocumentKind, "document_kind"))
    mime_type: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(Integer)
    page_count: Mapped[int | None] = mapped_column(Integer)
    storage_path: Mapped[str] = mapped_column(Text)
    source: Mapped[DocumentSource] = mapped_column(pg_enum(DocumentSource, "document_source"))
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    is_scanned: Mapped[bool] = mapped_column(Boolean, server_default=sql_text("false"))
    created_at: Mapped[dt.datetime] = now_column()


class Invoice(Base):
    """An incoming invoice: a document awaiting investigation, with no trusted values."""

    __tablename__ = "invoices"

    id: Mapped[uuid.UUID] = uuid_pk()
    dataset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("datasets.id"))
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), unique=True)
    received_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class ParsedDocument(Base):
    """Shared non-LLM preprocessing output: the identical input for both systems."""

    __tablename__ = "parsed_documents"
    __table_args__ = (UniqueConstraint("document_id", "parser_version"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"))
    parser_version: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    structure: Mapped[dict[str, Any]] = mapped_column(JSONB)
    coordinates: Mapped[dict[str, Any]] = mapped_column(JSONB)
    ocr_confidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    bank_tokens: Mapped[list[Any]] = mapped_column(JSONB, server_default=sql_text("'[]'::jsonb"))
    created_at: Mapped[dt.datetime] = now_column()


# ---------------------------------------------------------------- policy


class PolicyVersion(Base):
    __tablename__ = "policy_versions"
    __table_args__ = (
        Index(
            "uq_policy_versions_active",
            "is_active",
            unique=True,
            postgresql_where=sql_text("is_active"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    version: Mapped[str] = mapped_column(Text, unique=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    config_sha256: Mapped[str] = mapped_column(SHA256_HEX)
    document: Mapped[str] = mapped_column(Text)
    document_sha256: Mapped[str] = mapped_column(SHA256_HEX)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=sql_text("false"))
    created_at: Mapped[dt.datetime] = now_column()


class PolicyChunk(Base):
    __tablename__ = "policy_chunks"
    __table_args__ = (
        UniqueConstraint("policy_version_id", "section_id"),
        Index("ix_policy_chunks_tsv", "tsv", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    policy_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("policy_versions.id"))
    section_id: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', title || ' ' || text)", persisted=True)
    )
    # Filled in P7. No vector index: a few hundred chunks scan exactly and fast (§8).
    embedding: Mapped[Any | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS))


# ---------------------------------------------------------------- evaluation runs


class EvalRun(Base):
    __tablename__ = "eval_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    system: Mapped[EvalSystem] = mapped_column(pg_enum(EvalSystem, "eval_system"))
    dataset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("datasets.id"))
    subset: Mapped[str | None] = mapped_column(Text)
    repeat_index: Mapped[int] = mapped_column(Integer, server_default=sql_text("0"))
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    prompt_versions: Mapped[dict[str, Any]] = mapped_column(JSONB)
    policy_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("policy_versions.id"))
    git_sha: Mapped[str] = mapped_column(Text)
    status: Mapped[EvalRunStatus] = mapped_column(pg_enum(EvalRunStatus, "eval_run_status"))
    started_at: Mapped[dt.datetime] = now_column()
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------- cases and evidence


class Case(Base):
    __tablename__ = "cases"
    __table_args__ = (
        Index(
            "uq_cases_one_open_live_case",
            "invoice_id",
            unique=True,
            postgresql_where=sql_text("eval_run_id IS NULL AND status <> 'closed'"),
        ),
        Index(
            "ix_cases_queue",
            "status",
            sql_text("created_at DESC"),
            "id",
            postgresql_where=sql_text("eval_run_id IS NULL"),
        ),
        Index(
            "ix_cases_approvals",
            "status",
            "required_approver_tier",
            "required_approval_amount",
            postgresql_where=sql_text("status = 'pending_approval'"),
        ),
        Index("ix_cases_held", "status", postgresql_where=sql_text("status = 'held_for_approver'")),
        CheckConstraint("version > 0", name="positive_version"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id"))
    eval_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("eval_runs.id"))
    status: Mapped[CaseStatus] = mapped_column(
        pg_enum(CaseStatus, "case_status"), server_default=sql_text("'new'")
    )
    version: Mapped[int] = mapped_column(Integer, server_default=sql_text("1"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    submitted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    required_approval_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    required_approver_tier: Mapped[ApproverTier | None] = mapped_column(
        pg_enum(ApproverTier, "approver_tier")
    )
    evidence_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("investigation_runs.id", use_alter=True)
    )
    created_at: Mapped[dt.datetime] = now_column()
    updated_at: Mapped[dt.datetime] = now_column()
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class InvestigationRun(Base):
    __tablename__ = "investigation_runs"
    __table_args__ = (
        UniqueConstraint("case_id", "attempt"),
        CheckConstraint(
            "(kind = 'reopen') = (source_run_id IS NOT NULL)", name="reopen_has_source"
        ),
        CheckConstraint(
            "(status = 'failed') = (failure_reason IS NOT NULL)", name="failure_has_reason"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer)
    kind: Mapped[RunKind] = mapped_column(pg_enum(RunKind, "run_kind"))
    source_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("investigation_runs.id"))
    recheck_inputs: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[RunStatus] = mapped_column(pg_enum(RunStatus, "run_status"))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    thread_id: Mapped[str] = mapped_column(Text, unique=True)
    provider: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    prompt_versions: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    policy_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("policy_versions.id"))
    llm_call_count: Mapped[int] = mapped_column(Integer, server_default=sql_text("0"))
    tokens_in: Mapped[int] = mapped_column(Integer, server_default=sql_text("0"))
    tokens_out: Mapped[int] = mapped_column(Integer, server_default=sql_text("0"))
    execution_ms: Mapped[int | None] = mapped_column(Integer)
    quota_wait_ms: Mapped[int | None] = mapped_column(Integer)
    queue_wait_ms: Mapped[int | None] = mapped_column(Integer)
    end_to_end_ms: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[dt.datetime] = now_column()
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class Extraction(Base):
    __tablename__ = "extractions"
    __table_args__ = (
        CheckConstraint("bank_fp IS NULL OR bank_fp ~ '^[0-9a-f]{12}$'", name="bank_fp_format"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("investigation_runs.id"), unique=True)
    schema_version: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    field_confidence: Mapped[dict[str, Any]] = mapped_column(JSONB)
    coordinates: Mapped[dict[str, Any]] = mapped_column(JSONB)
    bank_fp: Mapped[str | None] = mapped_column(BANK_FP)
    bank_last4: Mapped[str | None] = mapped_column(LAST4)
    created_at: Mapped[dt.datetime] = now_column()


class LineMatch(Base):
    __tablename__ = "line_matches"
    __table_args__ = (
        UniqueConstraint("run_id", "invoice_line_no"),
        CheckConstraint(
            "similarity IS NULL OR (similarity >= 0 AND similarity <= 1)", name="similarity_range"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("investigation_runs.id"))
    invoice_line_no: Mapped[int] = mapped_column(Integer)
    po_line_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("po_lines.id"))
    method: Mapped[MatchMethod] = mapped_column(pg_enum(MatchMethod, "match_method"))
    similarity: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    justification: Mapped[str | None] = mapped_column(Text)


class CheckResult(Base):
    __tablename__ = "check_results"
    __table_args__ = (
        UniqueConstraint("run_id", "check_code", "subject_key"),
        CheckConstraint(
            "(status = 'inconclusive') = (inconclusive_reason IS NOT NULL)",
            name="inconclusive_has_reason",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("investigation_runs.id"), index=True)
    check_code: Mapped[CheckCode] = mapped_column(pg_enum(CheckCode, "check_code"))
    subject_key: Mapped[str] = mapped_column(Text)
    status: Mapped[CheckStatus] = mapped_column(pg_enum(CheckStatus, "check_status"))
    compared: Mapped[dict[str, Any]] = mapped_column(JSONB)
    tolerance: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    evidence_refs: Mapped[list[Any]] = mapped_column(JSONB)
    inconclusive_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = now_column()


class Finding(Base):
    __tablename__ = "findings"
    __table_args__ = (
        UniqueConstraint("run_id", "exception_type", "subject_key"),
        Index("ix_findings_type_case", "exception_type", "case_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("investigation_runs.id"))
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id"), index=True)
    exception_type: Mapped[FindingCode] = mapped_column(pg_enum(FindingCode, "finding_type"))
    subject_key: Mapped[str] = mapped_column(Text)
    severity: Mapped[Severity] = mapped_column(pg_enum(Severity, "severity"))
    explanation: Mapped[str] = mapped_column(Text)
    grounding_status: Mapped[GroundingStatus] = mapped_column(
        pg_enum(GroundingStatus, "grounding_status")
    )
    created_at: Mapped[dt.datetime] = now_column()


class FindingCheckResult(Base):
    __tablename__ = "finding_check_results"
    __table_args__ = (PrimaryKeyConstraint("finding_id", "check_result_id"),)

    finding_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("findings.id"))
    check_result_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("check_results.id"))


class FindingCitation(Base):
    __tablename__ = "finding_citations"
    __table_args__ = (PrimaryKeyConstraint("finding_id", "policy_chunk_id"),)

    finding_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("findings.id"))
    policy_chunk_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("policy_chunks.id"))
    quoted_text: Mapped[str] = mapped_column(Text)


class Proposal(Base):
    __tablename__ = "proposals"
    __table_args__ = (
        CheckConstraint(
            "source <> 'analyst' OR (changed_by IS NOT NULL"
            " AND reason IS NOT NULL AND length(btrim(reason)) > 0)",
            name="analyst_change_has_reason",
        ),
        Index("ix_proposals_current", "case_id", sql_text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("investigation_runs.id"))
    action: Mapped[ActionType] = mapped_column(pg_enum(ActionType, "action_type"))
    source: Mapped[ProposalSource] = mapped_column(pg_enum(ProposalSource, "proposal_source"))
    changed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str | None] = mapped_column(Text)
    policy_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("policy_versions.id"))
    digest: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[dt.datetime] = now_column()


class Decision(Base):
    __tablename__ = "decisions"
    __table_args__ = (
        CheckConstraint(
            "kind NOT IN ('dismiss', 'decline', 'reopen')"
            " OR (reason IS NOT NULL AND length(btrim(reason)) > 0)",
            name="reason_required",
        ),
        CheckConstraint("(actor_id IS NULL) = (kind = 'escalate')", name="system_only_escalates"),
        Index("ix_decisions_case", "case_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id"))
    kind: Mapped[DecisionKind] = mapped_column(pg_enum(DecisionKind, "decision_kind"))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("proposals.id"))
    reason: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    case_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = now_column()


class ExecutedAction(Base):
    __tablename__ = "executed_actions"

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id"))
    # NOT NULL + unique: one execution per approve decision (gate layer 3).
    decision_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("decisions.id"), unique=True)
    action: Mapped[ActionType] = mapped_column(pg_enum(ActionType, "action_type"))
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    executed_at: Mapped[dt.datetime] = now_column()


class ActionReversal(Base):
    __tablename__ = "action_reversals"

    id: Mapped[uuid.UUID] = uuid_pk()
    executed_action_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("executed_actions.id"), unique=True
    )
    decision_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("decisions.id"))
    created_at: Mapped[dt.datetime] = now_column()


# ---------------------------------------------------------------- resolutions


class SupportingDocument(Base):
    __tablename__ = "supporting_documents"

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id"), index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"))
    kind: Mapped[SupportingKind] = mapped_column(pg_enum(SupportingKind, "supporting_kind"))
    description: Mapped[str] = mapped_column(Text)
    added_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[dt.datetime] = now_column()


class BankVerification(Base):
    __tablename__ = "bank_verifications"
    __table_args__ = (
        CheckConstraint("contact_source = 'vendor_master'", name="contact_from_vendor_master"),
        CheckConstraint("verified_bank_fp ~ '^[0-9a-f]{12}$'", name="bank_fp_format"),
        CheckConstraint("verified_last4 ~ '^[0-9]{4}$'", name="last4_format"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id"), index=True)
    vendor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vendors.id"))
    channel: Mapped[VerificationChannel] = mapped_column(
        pg_enum(VerificationChannel, "verification_channel")
    )
    contact_source: Mapped[str] = mapped_column(Text)
    contact_reference: Mapped[str] = mapped_column(Text)
    verified_bank_fp: Mapped[str] = mapped_column(BANK_FP)
    verified_last4: Mapped[str] = mapped_column(LAST4)
    evidence_supporting_document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supporting_documents.id")
    )
    verified_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[dt.datetime] = now_column()


class FindingResolution(Base):
    __tablename__ = "finding_resolutions"
    __table_args__ = (
        CheckConstraint("length(btrim(summary)) > 0", name="summary_present"),
        Index("ix_finding_resolutions_current", "finding_id", sql_text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    finding_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("findings.id"))
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id"), index=True)
    resolution_type: Mapped[ResolutionType] = mapped_column(
        pg_enum(ResolutionType, "resolution_type")
    )
    summary: Mapped[str] = mapped_column(Text)
    structured: Mapped[dict[str, Any]] = mapped_column(JSONB)
    resolved_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    policy_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("policy_versions.id"))
    digest: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[dt.datetime] = now_column()


class FindingResolutionEvidence(Base):
    __tablename__ = "finding_resolution_evidence"
    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(supporting_document_id, bank_verification_id, erp_id) = 1"
            " AND (erp_id IS NULL) = (erp_entity IS NULL)",
            name="exactly_one_reference",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    resolution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("finding_resolutions.id"), index=True
    )
    supporting_document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("supporting_documents.id")
    )
    bank_verification_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("bank_verifications.id")
    )
    erp_entity: Mapped[str | None] = mapped_column(Text)
    erp_id: Mapped[uuid.UUID | None] = mapped_column()


class DecisionResolutionAcceptance(Base):
    __tablename__ = "decision_resolution_acceptances"
    __table_args__ = (PrimaryKeyConstraint("decision_id", "resolution_id"),)

    decision_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("decisions.id"))
    resolution_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("finding_resolutions.id"))
    resolution_digest: Mapped[bytes] = mapped_column(LargeBinary)


# ---------------------------------------------------------------- integrity and operations


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        UniqueConstraint("chain_id", "seq"),
        Index("ix_audit_events_case", "chain_id", "case_id", "seq"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    chain_id: Mapped[str] = mapped_column(Text)
    seq: Mapped[int] = mapped_column(BigInteger)
    occurred_at: Mapped[dt.datetime] = now_column()
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    case_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cases.id"))
    event_type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    prev_hash: Mapped[bytes] = mapped_column(LargeBinary)
    hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_claim", "run_after", postgresql_where=sql_text("status = 'queued'")),
        CheckConstraint("attempts >= 0 AND max_attempts > 0", name="attempt_counts"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    type: Mapped[JobType] = mapped_column(pg_enum(JobType, "job_type"))
    case_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cases.id"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=sql_text("'{}'::jsonb"))
    idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)
    status: Mapped[JobStatus] = mapped_column(
        pg_enum(JobStatus, "job_status"), server_default=sql_text("'queued'")
    )
    attempts: Mapped[int] = mapped_column(Integer, server_default=sql_text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, server_default=sql_text("5"))
    run_after: Mapped[dt.datetime] = now_column()
    leased_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = now_column()
    updated_at: Mapped[dt.datetime] = now_column()


class LlmQuotaWindow(Base):
    """Requests per provider, model and window: limits hold across restarts and workers."""

    __tablename__ = "llm_quota_windows"
    __table_args__ = (
        PrimaryKeyConstraint("provider", "model", "window_kind", "window_start"),
        CheckConstraint("window_kind IN ('minute', 'day')", name="window_kind"),
        CheckConstraint("requests >= 0", name="non_negative"),
    )

    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    window_kind: Mapped[str] = mapped_column(Text)
    window_start: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    requests: Mapped[int] = mapped_column(Integer)


class LlmCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("investigation_runs.id"))
    eval_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("eval_runs.id"))
    purpose: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    request_sha256: Mapped[str] = mapped_column(SHA256_HEX, index=True)
    request: Mapped[dict[str, Any]] = mapped_column(JSONB)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text)
    tokens_in: Mapped[int] = mapped_column(Integer, server_default=sql_text("0"))
    tokens_out: Mapped[int] = mapped_column(Integer, server_default=sql_text("0"))
    latency_ms: Mapped[int] = mapped_column(Integer, server_default=sql_text("0"))
    quota_wait_ms: Mapped[int] = mapped_column(Integer, server_default=sql_text("0"))
    created_at: Mapped[dt.datetime] = now_column()


# ---------------------------------------------------------------- evaluation


class DatasetItem(Base):
    __tablename__ = "dataset_items"
    __table_args__ = (
        CheckConstraint("num_nonnulls(invoice_id, document_id) = 1", name="one_subject"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    dataset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("datasets.id"), index=True)
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("invoices.id"))
    document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id"))
    # Readable only by the scorer role (column grant); never by inference (rules.md §2).
    labels: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = now_column()


class EvalItemResult(Base):
    __tablename__ = "eval_item_results"
    __table_args__ = (UniqueConstraint("eval_run_id", "dataset_item_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    eval_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("eval_runs.id"))
    dataset_item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dataset_items.id"))
    case_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cases.id"))
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    execution_ms: Mapped[int | None] = mapped_column(Integer)
    quota_wait_ms: Mapped[int | None] = mapped_column(Integer)
    end_to_end_ms: Mapped[int | None] = mapped_column(Integer)
    tokens_in: Mapped[int] = mapped_column(Integer, server_default=sql_text("0"))
    tokens_out: Mapped[int] = mapped_column(Integer, server_default=sql_text("0"))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = now_column()


class BaselineAction(Base):
    """The baseline's 'executed' actions (sandboxed); each counts as unapproved."""

    __tablename__ = "baseline_actions"

    id: Mapped[uuid.UUID] = uuid_pk()
    eval_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("eval_runs.id"))
    dataset_item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dataset_items.id"))
    action: Mapped[ActionType] = mapped_column(pg_enum(ActionType, "action_type"))
    args: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = now_column()


APPEND_ONLY_TABLES: tuple[str, ...] = (
    "audit_events",
    "decisions",
    "executed_actions",
    "action_reversals",
    "parsed_documents",
    "extractions",
    "line_matches",
    "check_results",
    "findings",
    "finding_check_results",
    "finding_citations",
    "proposals",
    "llm_calls",
    "supporting_documents",
    "bank_verifications",
    "finding_resolutions",
    "finding_resolution_evidence",
    "decision_resolution_acceptances",
    "baseline_actions",
)

"""Case endpoints (`06-api.md` §4): one handler per rule, two surfaces.

Each mutation is registered at `/api/v1/…` (JSON) and at its HTML twin
(a form post that redirects back to the case page). The handler is the same
function: the live-case check, then one transaction around one service call.
The service enforces role, version, state and every rule; this file decides
nothing.
"""

import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.requests import Request

from opspilot.cases import queries, service
from opspilot.cases.errors import ValidationFailedError
from opspilot.cases.resolutions import EvidenceEntry
from opspilot.config import Settings
from opspilot.domain.enums import (
    CaseStatus,
    DocumentKind,
    FindingCode,
    SupportingKind,
    UserRole,
)
from opspilot.web.auth import CsrfUser, CurrentUser, SignedIn, read_form
from opspilot.web.db import transaction
from opspilot.web.errors import json_response, wants_html
from opspilot.web.ratelimit import UPLOADS_PER_DAY, UPLOADS_PER_MINUTE, RateLimiter
from opspilot.web.schemas import (
    Approve,
    BankVerificationIn,
    ChangeProposal,
    CreateCase,
    Reasoned,
    Recheck,
    RecordResolution,
    Submit,
    Versioned,
    body,
)
from opspilot.web.uploads import check, read_limited, save_document, write_file

router = APIRouter()
API = "/api/v1"


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _detail(request: Request, user: SignedIn, case_id: uuid.UUID) -> dict[str, Any]:
    with transaction(request) as conn:
        return queries.case_detail(conn, user.actor, case_id)


def _done(
    request: Request, user: SignedIn, case_id: uuid.UUID, status: int = 200, extra: Any = None
) -> Response:
    """JSON: the updated `CaseDetail` (or the created record). HTML: back to the case page."""
    if wants_html(request):
        return RedirectResponse(f"/cases/{case_id}", status_code=303)
    return json_response(extra if extra is not None else _detail(request, user, case_id), status)


def _live(conn: Any, case_id: uuid.UUID) -> None:
    queries.require_live_case(conn, case_id)


# ---------------------------------------------------------------- reads


@router.get(f"{API}/me")
def me(user: CurrentUser) -> Response:
    return json_response(
        {
            "id": user.actor.id,
            "username": user.username,
            "display_name": user.display_name,
            "role": user.role,
            "approval_limit": user.actor.approval_limit,
            "approval_unlimited": user.actor.approval_unlimited,
            # The caller's own token, for the X-CSRF-Token header (the cookie is HttpOnly).
            "csrf_token": user.csrf_token,
        }
    )


@router.get(f"{API}/cases")
def list_cases(
    request: Request,
    user: CurrentUser,
    *,
    status: CaseStatus | None = None,
    exception_type: FindingCode | None = None,
    vendor_id: uuid.UUID | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> Response:
    with transaction(request) as conn:
        page = queries.list_cases(
            conn,
            user.actor,
            status=status,
            exception_type=exception_type,
            vendor_id=vendor_id,
            limit=limit,
            cursor=cursor,
        )
    return json_response({"items": page.items, "next_cursor": page.next_cursor})


@router.get(f"{API}/cases/{{case_id}}")
def get_case(request: Request, case_id: uuid.UUID, user: CurrentUser) -> Response:
    return json_response(_detail(request, user, case_id))


@router.get(f"{API}/cases/{{case_id}}/audit")
def case_audit(
    request: Request,
    user: CurrentUser,
    case_id: uuid.UUID,
    limit: int | None = None,
    cursor: str | None = None,
) -> Response:
    with transaction(request) as conn:
        return json_response(
            queries.case_audit(conn, user.actor, case_id, limit=limit, cursor=cursor)
        )


@router.get(f"{API}/cases/{{case_id}}/runs/{{run_id}}/trace")
def run_trace(
    request: Request, case_id: uuid.UUID, run_id: uuid.UUID, user: CurrentUser
) -> Response:
    with transaction(request) as conn:
        return json_response(queries.run_trace(conn, user.actor, case_id, run_id))


@router.get(f"{API}/approvals")
def approvals(
    request: Request,
    user: CurrentUser,
    status: CaseStatus | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> Response:
    if status not in {None, CaseStatus.PENDING_APPROVAL, CaseStatus.HELD_FOR_APPROVER}:
        raise ValidationFailedError(
            "status is pending_approval or held_for_approver", field="status"
        )
    with transaction(request) as conn:
        page = queries.list_approvals(
            conn,
            user.actor,
            held=status is CaseStatus.HELD_FOR_APPROVER,
            limit=limit,
            cursor=cursor,
        )
    return json_response({"items": page.items, "next_cursor": page.next_cursor})


# ---------------------------------------------------------------- T1 and T3


@router.post(f"{API}/cases")
@router.post("/cases", include_in_schema=False)
def create_case(
    request: Request,
    user: CsrfUser,
    data: Annotated[CreateCase, Depends(body(CreateCase))],
) -> Response:
    with transaction(request) as conn:
        queries.require_role(user.actor, UserRole.ANALYST)
        case_id = service.create_case(conn, user.actor.id, data.invoice_id)
    return _done(request, user, case_id, 201)


@router.post(f"{API}/cases/{{case_id}}/investigate")
@router.post("/cases/{case_id}/investigate", include_in_schema=False)
def retry_investigation(
    request: Request,
    user: CsrfUser,
    case_id: uuid.UUID,
    data: Annotated[Versioned, Depends(body(Versioned))],
) -> Response:
    with transaction(request) as conn:
        _live(conn, case_id)
        service.retry_investigation(conn, user.actor.id, case_id, data.expected_version)
    return _done(request, user, case_id, 202)


# ---------------------------------------------------------------- T6–T10


@router.put(f"{API}/cases/{{case_id}}/proposal")
@router.post("/cases/{case_id}/proposal", include_in_schema=False)
def change_proposal(
    request: Request,
    user: CsrfUser,
    case_id: uuid.UUID,
    data: Annotated[ChangeProposal, Depends(body(ChangeProposal))],
) -> Response:
    with transaction(request) as conn:
        _live(conn, case_id)
        service.change_proposal(
            conn,
            user.actor.id,
            case_id,
            data.expected_version,
            action=data.action,
            reason=data.reason,
        )
    return _done(request, user, case_id)


@router.post(f"{API}/cases/{{case_id}}/submit")
@router.post("/cases/{case_id}/submit", include_in_schema=False)
def submit(
    request: Request,
    user: CsrfUser,
    case_id: uuid.UUID,
    data: Annotated[Submit, Depends(body(Submit))],
) -> Response:
    with transaction(request) as conn:
        _live(conn, case_id)
        service.submit(conn, user.actor.id, case_id, data.expected_version, data.note)
    return _done(request, user, case_id)


@router.post(f"{API}/cases/{{case_id}}/dismiss")
@router.post("/cases/{case_id}/dismiss", include_in_schema=False)
def dismiss(
    request: Request,
    user: CsrfUser,
    case_id: uuid.UUID,
    data: Annotated[Reasoned, Depends(body(Reasoned))],
) -> Response:
    with transaction(request) as conn:
        _live(conn, case_id)
        service.dismiss(conn, user.actor.id, case_id, data.expected_version, data.reason)
    return _done(request, user, case_id)


# ---------------------------------------------------------------- T12, T13, T15


@router.post(f"{API}/cases/{{case_id}}/approve")
@router.post("/cases/{case_id}/approve", include_in_schema=False)
def approve(
    request: Request,
    user: CsrfUser,
    case_id: uuid.UUID,
    data: Annotated[Approve, Depends(body(Approve))],
) -> Response:
    accepted = [(a.resolution_id, bytes.fromhex(a.digest)) for a in data.accepted_resolutions]
    with transaction(request) as conn:
        _live(conn, case_id)
        service.approve(
            conn,
            user.actor.id,
            case_id,
            data.expected_version,
            accepted_resolutions=accepted,
            note=data.note,
        )
    return _done(request, user, case_id, 202)


@router.post(f"{API}/cases/{{case_id}}/decline")
@router.post("/cases/{case_id}/decline", include_in_schema=False)
def decline(
    request: Request,
    user: CsrfUser,
    case_id: uuid.UUID,
    data: Annotated[Reasoned, Depends(body(Reasoned))],
) -> Response:
    with transaction(request) as conn:
        _live(conn, case_id)
        service.decline(conn, user.actor.id, case_id, data.expected_version, data.reason)
    return _done(request, user, case_id)


@router.post(f"{API}/cases/{{case_id}}/reopen")
@router.post("/cases/{case_id}/reopen", include_in_schema=False)
def reopen(
    request: Request,
    user: CsrfUser,
    case_id: uuid.UUID,
    data: Annotated[Reasoned, Depends(body(Reasoned))],
) -> Response:
    with transaction(request) as conn:
        _live(conn, case_id)
        service.reopen(conn, user.actor.id, case_id, data.expected_version, data.reason)
    return _done(request, user, case_id)


# ---------------------------------------------------------------- T16, T17 and evidence


@router.post(f"{API}/cases/{{case_id}}/findings/{{finding_id}}/resolutions")
@router.post("/cases/{case_id}/findings/{finding_id}/resolutions", include_in_schema=False)
def record_resolution(
    request: Request,
    user: CsrfUser,
    case_id: uuid.UUID,
    finding_id: uuid.UUID,
    data: Annotated[RecordResolution, Depends(body(RecordResolution))],
) -> Response:
    evidence = [
        EvidenceEntry(e.supporting_document_id, e.bank_verification_id, e.erp_entity, e.erp_id)
        for e in data.evidence
    ]
    with transaction(request) as conn:
        _live(conn, case_id)
        resolution_id, digest = service.record_resolution(
            conn,
            user.actor.id,
            case_id,
            data.expected_version,
            finding_id=finding_id,
            resolution_type=data.resolution_type,
            summary=data.summary,
            evidence=evidence,
            structured=data.structured,
        )
    return _done(request, user, case_id, 201, {"resolution_id": resolution_id, "digest": digest})


@router.post(f"{API}/cases/{{case_id}}/recheck")
@router.post("/cases/{case_id}/recheck", include_in_schema=False)
def recheck(
    request: Request,
    user: CsrfUser,
    case_id: uuid.UUID,
    data: Annotated[Recheck, Depends(body(Recheck))],
) -> Response:
    with transaction(request) as conn:
        _live(conn, case_id)
        service.request_recheck(
            conn,
            user.actor.id,
            case_id,
            data.expected_version,
            data.reason,
            replacement_document_id=data.replacement_document_id,
            po_number=data.po_number,
        )
    return _done(request, user, case_id, 202)


@router.post(f"{API}/cases/{{case_id}}/bank-verifications")
@router.post("/cases/{case_id}/bank-verifications", include_in_schema=False)
def record_bank_verification(
    request: Request,
    user: CsrfUser,
    case_id: uuid.UUID,
    data: Annotated[BankVerificationIn, Depends(body(BankVerificationIn))],
) -> Response:
    with transaction(request) as conn:
        _live(conn, case_id)
        verification_id, _fp, last4 = service.record_bank_verification(
            conn,
            user.actor.id,
            case_id,
            data.expected_version,
            channel=data.channel,
            contact_source=data.contact_source,
            contact_reference=data.contact_reference,
            verified_account_number=data.verified_account_number.get_secret_value(),
            evidence_document_id=data.evidence_document_id,
            bank_key=_settings(request).bank_hmac_key.get_secret_value(),
        )
    # The fingerprint stays on the server: clients see only the last four digits.
    return _done(
        request,
        user,
        case_id,
        201,
        {"bank_verification_id": verification_id, "account": f"{queries.MASK}{last4}"},
    )


def _limiter(request: Request) -> RateLimiter:
    limiter: RateLimiter = request.app.state.limiter
    return limiter


async def _file(request: Request, user: SignedIn) -> Any:
    """Rate-limit, then read and check the uploaded file (CPU work off the event loop)."""
    _limiter(request).hit(
        (("upload-minute", str(user.actor.id)), UPLOADS_PER_MINUTE),
        (("upload-day", str(user.actor.id)), UPLOADS_PER_DAY),
    )
    form = await read_form(request)
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        raise ValidationFailedError("Choose a file to upload.", field="file")
    data = await read_limited(upload, _settings(request).upload_max_bytes)
    checked = await run_in_threadpool(check, data)
    store = Path(_settings(request).document_store_path)
    path = await run_in_threadpool(write_file, store, checked)
    return form, checked, path


@router.post(f"{API}/cases/{{case_id}}/supporting-documents")
@router.post("/cases/{case_id}/supporting-documents", include_in_schema=False)
async def add_supporting_document(request: Request, case_id: uuid.UUID, user: CsrfUser) -> Response:
    queries.require_role(user.actor, UserRole.ANALYST)
    form, checked, path = await _file(request, user)
    kind_value = form.get("kind")
    if kind_value not in {k.value for k in SupportingKind}:
        raise ValidationFailedError("Choose the kind of document.", field="kind")
    description = form.get("description")

    def save() -> tuple[uuid.UUID, uuid.UUID]:
        with transaction(request) as conn:
            _live(conn, case_id)
            document_id = save_document(conn, checked, path, DocumentKind.SUPPORTING, user.actor.id)
            supporting_id = service.add_supporting_document(
                conn,
                user.actor.id,
                case_id,
                document_id=document_id,
                kind=SupportingKind(str(kind_value)),
                description=description if isinstance(description, str) else "",
            )
        return supporting_id, document_id

    supporting_id, document_id = await run_in_threadpool(save)
    return await run_in_threadpool(
        _done,
        request,
        user,
        case_id,
        201,
        {"supporting_document_id": supporting_id, "document_id": document_id},
    )


@router.post(f"{API}/uploads")
@router.post("/uploads", include_in_schema=False)
async def upload(request: Request, user: CsrfUser) -> Response:
    """A synthetic invoice file becomes a demo-dataset invoice and a case (T1)."""
    queries.require_role(user.actor, UserRole.ANALYST)
    _form, checked, path = await _file(request, user)

    def save() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
        with transaction(request) as conn:
            document_id = save_document(conn, checked, path, DocumentKind.UPLOAD, user.actor.id)
            invoice_id, case_id = service.create_upload_case(conn, user.actor.id, document_id)
        return document_id, invoice_id, case_id

    document_id, invoice_id, case_id = await run_in_threadpool(save)
    return await run_in_threadpool(
        _done,
        request,
        user,
        case_id,
        201,
        {
            "document_id": document_id,
            "invoice_id": invoice_id,
            "case_id": case_id,
            **queries.redaction(checked.mime_type),
        },
    )

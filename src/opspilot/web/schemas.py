"""Request bodies, validated at the boundary with `extra = forbid` (`rules.md` §7).

One parser serves both surfaces: JSON for `/api/v1`, form fields for the HTML
twins. Repeated form fields carry lists (`accepted_resolution` = "id:digest",
`evidence` = "supporting:<id>" | "bank:<id>" | "erp:<entity>:<id>") and
`structured.<name>` fields build the structured resolution values.
"""

import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator
from starlette.datastructures import FormData
from starlette.requests import Request

from opspilot.cases.errors import ServiceError, ValidationFailedError
from opspilot.domain.enums import ActionType, ResolutionType
from opspilot.web.auth import CSRF_FIELD, is_form, read_form

Text = Annotated[str, Field(max_length=5000)]
Version = Annotated[int, Field(ge=1)]


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @classmethod
    def from_form(cls, form: FormData) -> dict[str, Any]:
        """Single-valued fields; empty ones are left out, as if not sent."""
        return {k: v for k, v in form.multi_items() if k != CSRF_FIELD and isinstance(v, str) and v}


class CreateCase(Body):
    invoice_id: uuid.UUID


class Versioned(Body):
    expected_version: Version


class Reasoned(Body):
    reason: Text
    expected_version: Version


class ChangeProposal(Body):
    action: ActionType
    reason: Text
    expected_version: Version


class Submit(Body):
    expected_version: Version
    note: Text | None = None


class Accepted(Body):
    resolution_id: uuid.UUID
    digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class Approve(Body):
    expected_version: Version
    accepted_resolutions: list[Accepted] = []
    note: Text | None = None

    @classmethod
    def from_form(cls, form: FormData) -> dict[str, Any]:
        data = super().from_form(form)
        data.pop("accepted_resolution", None)
        data["accepted_resolutions"] = [
            dict(zip(("resolution_id", "digest"), str(v).split(":", 1), strict=False))
            for v in form.getlist("accepted_resolution")
        ]
        return data


class EvidenceRef(Body):
    supporting_document_id: uuid.UUID | None = None
    bank_verification_id: uuid.UUID | None = None
    erp_entity: Annotated[str, Field(max_length=64)] | None = None
    erp_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        given = [self.supporting_document_id, self.bank_verification_id, self.erp_id]
        if sum(v is not None for v in given) != 1 or (self.erp_id is None) != (
            self.erp_entity is None
        ):
            raise ValueError("each evidence entry references exactly one record")
        return self


def _evidence_from_form(value: str) -> dict[str, str]:
    kind, _, rest = value.partition(":")
    if kind == "supporting":
        return {"supporting_document_id": rest}
    if kind == "bank":
        return {"bank_verification_id": rest}
    entity, _, erp_id = rest.partition(":")
    return {"erp_entity": entity, "erp_id": erp_id}


class RecordResolution(Body):
    resolution_type: ResolutionType
    summary: Text
    evidence: list[EvidenceRef] = []
    structured: dict[str, Annotated[str, Field(max_length=200)]] = {}
    expected_version: Version

    @classmethod
    def from_form(cls, form: FormData) -> dict[str, Any]:
        data = {k: v for k, v in super().from_form(form).items() if not k.startswith("structured.")}
        data.pop("evidence", None)
        data["evidence"] = [_evidence_from_form(str(v)) for v in form.getlist("evidence") if v]
        data["structured"] = {
            k.removeprefix("structured."): v
            for k, v in form.multi_items()
            if k.startswith("structured.") and isinstance(v, str) and v
        }
        return data


class Recheck(Body):
    reason: Text
    replacement_document_id: uuid.UUID | None = None
    po_number: Annotated[str, Field(max_length=64)] | None = None
    expected_version: Version


class BankVerificationIn(Body):
    channel: Annotated[str, Field(max_length=64)]
    contact_source: Annotated[str, Field(max_length=64)]
    contact_reference: Annotated[str, Field(max_length=500)]
    # SecretStr: never in a repr, a log line or an error message.
    verified_account_number: SecretStr
    evidence_document_id: uuid.UUID
    expected_version: Version


class BadRequestError(ServiceError):
    status = 400
    code = "bad_request"


def _fields(exc: ValidationError) -> list[dict[str, str]]:
    # Field names and messages only: inputs (an account number, say) are never echoed.
    return [
        {"field": ".".join(str(p) for p in e["loc"]), "problem": e["msg"]} for e in exc.errors()
    ]


def body[M: Body](model: type[M]) -> Callable[[Request], Awaitable[M]]:
    """A dependency that reads the request as JSON or as a form into `model`."""

    async def parse(request: Request) -> M:
        if is_form(request):
            data: Any = model.from_form(await read_form(request))
        else:
            raw = await request.body()
            try:
                data = json.loads(raw) if raw.strip() else {}
            except ValueError:
                raise BadRequestError("The request body isn't valid JSON.") from None
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            raise ValidationFailedError("Some fields are invalid.", fields=_fields(exc)) from None

    return parse

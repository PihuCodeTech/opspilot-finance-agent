"""HTTP tests against the real app, inside the test's rolled-back transaction.

The app's transaction factory is a savepoint on the test connection, so every
request sees what the test wrote and nothing survives the test. After the world
is built the connection switches to the `opspilot_app` role, so the requests
also prove the app role's grants are enough.
"""

import contextlib
import ctypes
import io
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from httpx2 import Response
from pydantic import SecretStr
from sqlalchemy import Connection, text

from opspilot.config import get_settings
from opspilot.extraction.preprocess import PARSER_VERSION
from opspilot.web.app import create_app
from opspilot.web.ratelimit import RateLimiter
from tests.integration import case_support as cs

PASSWORD = "correct horse battery staple"
_HASH = PasswordHasher().hash(PASSWORD)


@dataclass
class Clock:
    now: float = 1000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Web:
    world: cs.World
    store: Path
    clock: Clock
    max_bytes: int
    responses: list[Response] = field(default_factory=list)

    def app(self) -> Any:
        conn = self.world.conn

        @contextlib.contextmanager
        def savepoint() -> Iterator[Connection]:
            with conn.begin_nested():
                yield conn

        settings = get_settings().model_copy(
            update={
                "document_store_path": str(self.store),
                "bank_hmac_key": SecretStr(cs.BANK_KEY),
                "upload_max_bytes": self.max_bytes,
            }
        )
        return create_app(savepoint, settings=settings, limiter=RateLimiter(self.clock))

    def client(self, user_id: uuid.UUID | None = None) -> "Client":
        client = Client(self.app(), base_url="https://testserver", web=self)
        if user_id is not None:
            client.sign_in(self.username(user_id))
        return client

    def username(self, user_id: uuid.UUID) -> str:
        return str(
            self.world.conn.execute(
                text("SELECT username FROM users WHERE id = :u"), {"u": user_id}
            ).scalar_one()
        )


class Client(TestClient):
    """A browser-like session for one user; records every response for leak checks."""

    def __init__(self, app: Any, *, base_url: str, web: Web) -> None:
        super().__init__(
            app, base_url=base_url, raise_server_exceptions=False, follow_redirects=False
        )
        self.web = web
        self.csrf = ""

    def request(self, method: str, url: Any, *args: Any, **kwargs: Any) -> Response:
        response = super().request(method, url, *args, **kwargs)
        self.web.responses.append(response)
        return response

    def sign_in(self, username: str, password: str = PASSWORD) -> Response:
        response = self.post("/auth/login", json={"username": username, "password": password})
        if response.status_code == 204:
            self.csrf = self.get("/api/v1/me").json()["csrf_token"]
        return response

    def mutate(self, method: str, url: str, **kwargs: Any) -> Response:
        """A mutation with this session's CSRF header."""
        headers = {"X-CSRF-Token": self.csrf, **kwargs.pop("headers", {})}
        return self.request(method, url, headers=headers, **kwargs)

    def version(self, case_id: uuid.UUID) -> int:
        return int(self.get(f"/api/v1/cases/{case_id}").json()["version"])


def make_web(conn: Connection, store: Path, *, max_bytes: int = 10 * 1024 * 1024) -> Web:
    world = cs.make_world(conn)
    conn.execute(text("UPDATE users SET password_hash = :h"), {"h": _HASH})
    conn.execute(
        text("INSERT INTO datasets (name, version, split) VALUES (:n, '1', 'demo')"),
        {"n": f"demo-{uuid.uuid4().hex[:8]}"},
    )
    conn.execute(text("SET LOCAL ROLE opspilot_app"))
    return Web(world, store, Clock(), max_bytes)


def pdf_bytes(pages: int = 1) -> bytes:
    document = pdfium.PdfDocument.new()
    for _ in range(pages):
        document.new_page(612, 792)
    out = io.BytesIO()
    document.save(out)
    document.close()
    # A comment makes every generated file unique, so uploads never collide.
    return out.getvalue() + f"\n%{uuid.uuid4().hex}\n".encode()


def png_bytes() -> bytes:
    from PIL import Image  # noqa: PLC0415

    out = io.BytesIO()
    Image.new("RGB", (40, 30), (255, 255, 255)).save(out, format="PNG")
    return out.getvalue()


def text_pdf(*lines: tuple[str, float, float]) -> bytes:
    """A one-page PDF with a real text layer: each line is (text, x, y) in points."""
    document = pdfium.PdfDocument.new()
    page = document.new_page(612, 792)
    for content, x, y in lines:
        obj = pdfium_raw.FPDFPageObj_NewTextObj(document.raw, b"Helvetica", ctypes.c_float(12.0))
        buffer = ctypes.create_string_buffer((content + "\0").encode("utf-16-le"))
        pdfium_raw.FPDFText_SetText(obj, ctypes.cast(buffer, ctypes.POINTER(pdfium_raw.FPDF_WCHAR)))
        pdfium_raw.FPDFPageObj_Transform(obj, 1, 0, 0, 1, x, y)
        pdfium_raw.FPDFPage_InsertObject(page.raw, obj)
    pdfium_raw.FPDFPage_GenerateContent(page.raw)
    out = io.BytesIO()
    document.save(out)
    document.close()
    return out.getvalue() + f"\n%{uuid.uuid4().hex}\n".encode()


def parse_row(conn: Connection, document_id: Any, coordinates: dict[str, Any]) -> None:
    """What the worker's parse would store (current parser version), with these coordinates."""
    conn.execute(
        text(
            "INSERT INTO parsed_documents (document_id, parser_version, text, structure,"
            " coordinates) VALUES (:d, :v, 'parsed', '{}', CAST(:c AS jsonb))"
        ),
        {"d": document_id, "v": PARSER_VERSION, "c": json.dumps(coordinates)},
    )

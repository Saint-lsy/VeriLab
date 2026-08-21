from __future__ import annotations

import asyncio
import json
import secrets
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ValidationError

from .bundle import AuditBundle
from .config import Settings
from .i18n import LANGUAGE_COOKIE, SUPPORTED_LANGUAGES, make_labeler, make_translator
from .models import ExperimentSpec
from .service import InvalidState, NotFound, ServiceError, VeriLabService

PACKAGE_ROOT = Path(__file__).parent


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20000)


class NoteRequest(BaseModel):
    note: str = Field(min_length=1, max_length=20000)


class WithdrawRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=4000)


def create_app(
    service: VeriLabService | None = None,
    *,
    settings: Settings | None = None,
    start_worker: bool = True,
) -> FastAPI:
    settings = settings or Settings.load()
    service = service or VeriLabService(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if start_worker:
            service.start()
        yield
        if start_worker:
            service.stop()

    app = FastAPI(title="VeriLab", version="0.1.0", lifespan=lifespan)
    app.state.service = service
    app.state.settings = settings
    templates = Jinja2Templates(directory=PACKAGE_ROOT / "templates")
    app.mount("/static", StaticFiles(directory=PACKAGE_ROOT / "static"), name="static")

    def page_context(request: Request, **values: Any) -> dict[str, Any]:
        language = request.cookies.get(LANGUAGE_COOKIE, "en")
        if language not in SUPPORTED_LANGUAGES:
            language = "en"
        return {
            **values,
            "language": language,
            "html_language": "zh-CN" if language == "zh" else "en",
            "tr": make_translator(language),
            "label": make_labeler(language),
        }

    @app.middleware("http")
    async def csrf_cookie(request: Request, call_next):
        unsafe = request.method not in {"GET", "HEAD", "OPTIONS"}
        path = request.url.path
        narrow_agent_mutation = request.method == "POST" and (
            path == "/api/experiments"
            or (path.startswith("/api/runs/") and path.endswith("/cancel"))
        )
        authorized = narrow_agent_mutation and (
            request.headers.get("authorization") == f"Bearer {settings.capability_token}"
        )
        if unsafe and not authorized:
            cookie = request.cookies.get("verilab_csrf")
            header = request.headers.get("x-csrf-token")
            if not cookie or not header or not secrets.compare_digest(cookie, header):
                return JSONResponse({"detail": "CSRF validation failed"}, status_code=403)
        response = await call_next(request)
        if "verilab_csrf" not in request.cookies:
            response.set_cookie(
                "verilab_csrf",
                secrets.token_urlsafe(24),
                httponly=False,
                samesite="strict",
                secure=False,
            )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'"
        )
        return response

    def capability(authorization: Annotated[str | None, Header()] = None) -> None:
        if authorization != f"Bearer {settings.capability_token}":
            raise HTTPException(status_code=401, detail="local capability required")

    @app.exception_handler(ServiceError)
    async def service_error(_request: Request, exc: ServiceError):
        code = 404 if isinstance(exc, NotFound) else 409 if isinstance(exc, InvalidState) else 400
        return JSONResponse({"detail": str(exc)}, status_code=code)

    @app.exception_handler(ValidationError)
    async def validation_error(_request: Request, exc: ValidationError):
        return JSONResponse({"detail": exc.errors()}, status_code=422)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        experiments = service.list_experiments()
        counts = Counter(item["status"] for item in experiments)
        leaderboard = service.leaderboard(service.policy.comparison_key)
        lineage = service.experiment_lineage(service.policy.comparison_key)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            page_context(
                request,
                experiments=experiments,
                counts=counts,
                leaderboard=leaderboard,
                lineage=lineage,
                comparison_key=service.policy.comparison_key,
            ),
        )

    @app.get("/experiments/{experiment_id}", response_class=HTMLResponse)
    async def experiment_page(request: Request, experiment_id: str):
        return templates.TemplateResponse(
            request,
            "experiment.html",
            page_context(request, experiment=service.get_experiment(experiment_id)),
        )

    @app.get("/audit", response_class=HTMLResponse)
    async def audit_page(request: Request):
        experiments = service.list_experiments()
        inbox_rows = [
            item
            for item in experiments
            if item["status"]
            in {
                "REVIEW_PENDING",
                "REVIEW_BLOCKED",
                "NEEDS_HUMAN",
                "VERIFICATION_FAILED",
                "REJECTED",
            }
            or item["evidence_health"] != "healthy"
        ]
        inbox = [service.get_experiment(item["id"]) for item in inbox_rows]
        return templates.TemplateResponse(
            request,
            "audit.html",
            page_context(request, experiments=inbox),
        )

    @app.get("/language/{language}")
    async def set_language(
        language: str,
        next_path: Annotated[str, Query(alias="next")] = "/",
    ):
        if language not in SUPPORTED_LANGUAGES:
            raise HTTPException(status_code=404, detail="unsupported interface language")
        destination = (
            next_path if next_path.startswith("/") and not next_path.startswith("//") else "/"
        )
        response = RedirectResponse(destination, status_code=303)
        response.set_cookie(
            LANGUAGE_COOKIE,
            language,
            max_age=365 * 24 * 60 * 60,
            httponly=True,
            samesite="strict",
            secure=False,
        )
        return response

    @app.post("/api/chat/messages")
    async def chat(body: ChatRequest):
        return await asyncio.to_thread(service.chat, body.message)

    @app.post("/api/experiments", dependencies=[Depends(capability)])
    async def submit(body: dict[str, Any]):
        return service.submit(ExperimentSpec.model_validate(body))

    @app.get("/api/experiments")
    async def experiments(status: str | None = None):
        return {"experiments": service.list_experiments(status=status)}

    @app.get("/api/experiments/{experiment_id}")
    async def experiment(experiment_id: str):
        return service.get_experiment(experiment_id)

    @app.get("/api/leaderboard")
    async def leaderboard(comparison_key: str | None = None):
        return {"entries": service.leaderboard(comparison_key)}

    @app.get("/api/lineage")
    async def lineage(comparison_key: str | None = None):
        return service.experiment_lineage(comparison_key)

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel(run_id: str):
        return service.cancel(run_id)

    @app.post("/api/reviews/{review_id}/retry")
    async def retry(review_id: str):
        return await asyncio.to_thread(service.retry_review, review_id)

    @app.post("/api/experiments/{experiment_id}/notes")
    async def notes(experiment_id: str, body: NoteRequest):
        service.add_note(experiment_id, body.note)
        return {"ok": True}

    @app.post("/api/experiments/{experiment_id}/withdraw")
    async def withdraw(experiment_id: str, body: WithdrawRequest):
        service.withdraw(experiment_id, body.reason)
        return {"ok": True}

    @app.get("/api/events")
    async def events(
        request: Request,
        after: Annotated[int, Query(ge=0)] = 0,
        once: bool = False,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ):
        try:
            header_cursor = int(last_event_id or 0)
        except ValueError:
            header_cursor = 0
        cursor = max(after, header_cursor)

        async def stream():
            nonlocal cursor
            while True:
                rows = service.ledger.list(after=cursor, limit=500)
                for row in rows:
                    cursor = row["seq"]
                    payload = json.dumps(row, separators=(",", ":"), ensure_ascii=False)
                    yield f"id: {cursor}\nevent: {row['event_type']}\ndata: {payload}\n\n"
                if once:
                    break
                if await request.is_disconnected():
                    break
                if not rows:
                    yield ": keepalive\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/audit/verify")
    async def audit_verify():
        return service.audit_verify()

    @app.get("/api/experiments/{experiment_id}/bundle")
    async def bundle(experiment_id: str):
        directory = service.latest_review_bundle(experiment_id)
        return Response(
            AuditBundle.export(directory),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{experiment_id}-audit-bundle.zip"'
            },
        )

    return app

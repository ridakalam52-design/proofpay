"""FastAPI entrypoint: HTML pages + JSON API."""
from __future__ import annotations

import logging
import mimetypes
import threading
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .ai import GeminiAgent, MockAgent
from .config import Settings
from .db import DB
from .paypal import MockPayPal, PayPalClient
from .services import FlowError, ProofPay

APP_DIR = Path(__file__).resolve().parent

log = logging.getLogger("proofpay")
if not log.handlers:  # make INFO lines visible in Render / uvicorn logs
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(levelname)s:     [proofpay] %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False


def log_available_flash_models(agent: GeminiAgent) -> None:
    """One startup line listing 'flash' models visible to the key (never prints the key). Best effort."""
    try:
        names = agent.list_flash_models(timeout=8.0)
        log.info("Gemini models with 'flash' available to this key: %s (using %s%s)",
                 ", ".join(names) or "none", agent.model,
                 f", fallback {agent.fallback_model}" if agent.fallback_model else "")
    except Exception as e:  # noqa: BLE001 - diagnostics only
        log.info("Could not list Gemini models at startup: %s", type(e).__name__)


class CreateDeal(BaseModel):
    description: str


class UpdateDeal(BaseModel):
    worker_name: str | None = None
    task: str | None = None
    amount: str | float | None = None
    deadline: str | None = None
    checklist: list[str] | None = None


class AuthorizeBody(BaseModel):
    orderID: str


class NoteBody(BaseModel):
    note: str = ""


def create_app(settings: Settings | None = None, agent=None, gateway=None) -> FastAPI:
    settings = settings or Settings()
    db = DB(settings.database_path)
    if agent is None:
        if settings.ai_mock:
            agent = MockAgent()
        else:
            agent = GeminiAgent(settings.gemini_api_key, settings.gemini_model,
                                fallback_model=settings.gemini_fallback_model,
                                timeout=settings.gemini_timeout, budget=settings.gemini_total_budget)
            # diagnostics in a background thread so startup is never blocked
            threading.Thread(target=log_available_flash_models, args=(agent,), name="proofpay-models",
                             daemon=True).start()
    if gateway is None:
        gateway = MockPayPal() if settings.paypal_mock else PayPalClient(
            settings.paypal_client_id, settings.paypal_client_secret, settings.paypal_base)
    svc = ProofPay(settings, db, agent, gateway)

    app = FastAPI(title="ProofPay", description="Pay when the work is proven.", version="1.0.0")
    app.state.svc = svc
    app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

    @app.exception_handler(FlowError)
    async def flow_error(_: Request, exc: FlowError):
        return JSONResponse({"error": str(exc)}, status_code=exc.status_code)

    def ctx(request: Request, **kw):
        return {
            "request": request,
            "modes": settings.summary(),
            "paypal_mock": settings.paypal_mock,
            "paypal_client_id": settings.paypal_client_id,
            **kw,
        }

    def client_deal(token: str) -> dict:
        deal = db.deal_by("client_token", token)
        if not deal:
            raise HTTPException(404, "Deal not found")
        return deal

    def worker_deal(token: str) -> dict:
        deal = db.deal_by("worker_token", token)
        if not deal:
            raise HTTPException(404, "Deal not found")
        return deal

    # ------------------------------------------------------------- pages
    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        return templates.TemplateResponse(request, "index.html", ctx(request))

    @app.get("/c/{token}", response_class=HTMLResponse)
    def client_page(request: Request, token: str):
        deal = client_deal(token)
        return templates.TemplateResponse(request, "client.html", ctx(request, token=token, deal_id=deal["id"]))

    @app.get("/w/{token}", response_class=HTMLResponse)
    def worker_page(request: Request, token: str):
        deal = worker_deal(token)
        return templates.TemplateResponse(request, "worker.html", ctx(request, token=token, deal_id=deal["id"]))

    @app.get("/media/{deal_id}/{fname}")
    def media(deal_id: str, fname: str):
        base = Path(settings.upload_dir).resolve()
        path = (base / deal_id / fname).resolve()
        if base not in path.parents or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream")

    @app.get("/healthz")
    def health():
        return {"ok": True, **settings.summary()}

    # --------------------------------------------------------------- API
    @app.post("/api/deals")
    def api_create(body: CreateDeal):
        deal = svc.create_deal(body.description)
        view = svc.view(deal, "client")
        view["clarifications"] = deal.get("clarifications", [])
        return view

    @app.get("/api/c/{token}")
    def api_client_get(token: str):
        return svc.view(client_deal(token), "client")

    @app.patch("/api/c/{token}")
    def api_client_update(token: str, body: UpdateDeal):
        return svc.view(svc.update_draft(client_deal(token), body.model_dump()), "client")

    @app.post("/api/c/{token}/paypal/order")
    def api_order(token: str):
        return svc.create_order(client_deal(token))

    @app.post("/api/c/{token}/paypal/authorize")
    def api_authorize(token: str, body: AuthorizeBody):
        return svc.view(svc.authorize(client_deal(token), body.orderID), "client")

    @app.post("/api/c/{token}/release")
    def api_release(token: str):
        return svc.view(svc.release(client_deal(token)), "client")

    @app.post("/api/c/{token}/void")
    def api_void(token: str):
        return svc.view(svc.void(client_deal(token)), "client")

    @app.post("/api/c/{token}/request-changes")
    def api_changes(token: str, body: NoteBody):
        return svc.view(svc.request_changes(client_deal(token), body.note), "client")

    @app.get("/api/w/{token}")
    def api_worker_get(token: str):
        return svc.view(worker_deal(token), "worker")

    @app.post("/api/w/{token}/proof")
    async def api_proof(token: str, note: str = Form(""), photos: list[UploadFile] = File(default=[])):
        deal = worker_deal(token)
        files = []
        for f in photos[: settings.max_photos + 1]:
            blob = await f.read()
            mime = (f.content_type or mimetypes.guess_type(f.filename or "")[0] or "").lower()
            if mime == "image/jpg":
                mime = "image/jpeg"
            files.append((f.filename or "photo", mime, blob))
        # store photos off the event loop; slow AI review continues in the background
        from starlette.concurrency import run_in_threadpool
        deal = await run_in_threadpool(svc.submit_proof, deal, note, files)
        return svc.view(deal, "worker")

    return app


app = create_app()

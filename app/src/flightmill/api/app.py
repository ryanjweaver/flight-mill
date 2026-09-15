"""Local web application around one shared acquisition service."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import tempfile
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from flightmill.acquisition.service import AcquisitionService
from flightmill.storage.trial_writer import discover_trials

LOGGER = logging.getLogger(__name__)
UI_ROOT = Path(__file__).resolve().parents[1] / "ui"
COOKIE_NAME = "flightmill_session"


def create_app(output_dir: Path, *, service: AcquisitionService | None = None) -> FastAPI:
    """Create one instrument owner. Uvicorn must run with one worker."""
    acquisition = service if service is not None else AcquisitionService(output_dir)
    token = secrets.token_urlsafe(32)
    cookie_name = f"{COOKIE_NAME}_{secrets.token_hex(6)}"
    known_roots = {Path(output_dir).resolve()}

    def snapshot() -> dict:
        value = acquisition.snapshot()
        if value.get("output_dir"):
            known_roots.add(Path(value["output_dir"]).resolve())
        return value

    async def ticker() -> None:
        while True:
            try:
                await asyncio.to_thread(acquisition.tick)
            except Exception:
                # The service owns failure handling and preservation of partial files.
                LOGGER.exception("Acquisition tick failed")
            await asyncio.sleep(0.02)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(ticker(), name="flightmill-acquisition")
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await asyncio.to_thread(acquisition.close)

    app = FastAPI(title="Flight Mill", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.acquisition = acquisition
    templates = Jinja2Templates(directory=str(UI_ROOT / "templates"))

    def local_request(headers) -> bool:
        host = headers.get("host", "")
        try:
            target = urlsplit("http://" + host)
            if target.hostname not in {"127.0.0.1", "localhost"} or target.username:
                return False
            origin = headers.get("origin")
            if origin:
                parsed = urlsplit(origin)
                if (parsed.scheme not in {"http", "https"}
                        or parsed.netloc.lower() != host.lower()):
                    return False
        except ValueError:
            return False
        return headers.get("sec-fetch-site", "same-origin") not in {"cross-site"}

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if not local_request(request.headers):
            return JSONResponse({"detail": "This application accepts local requests only."},
                                status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            supplied = request.headers.get("x-flightmill-token", "")
            if not secrets.compare_digest(supplied, token):
                return JSONResponse({"detail": "Refresh the application to reconnect."},
                                    status_code=403)
        if request.url.path.startswith("/api/") and request.url.path != "/api/session":
            supplied = request.cookies.get(cookie_name, "")
            header_token = request.headers.get("x-flightmill-token", "")
            if not (secrets.compare_digest(supplied, token)
                    or secrets.compare_digest(header_token, token)):
                return JSONResponse({"detail": "Open the application to start a local session."},
                                    status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    @app.get("/")
    def index(request: Request):
        return templates.TemplateResponse(request=request, name="index.html", context={})

    @app.get("/health")
    def health():
        return {"application": "flightmill", "mode": acquisition.source, "ready": True}

    @app.get("/api/session")
    def session():
        response = JSONResponse({"token": token})
        response.set_cookie(cookie_name, token, httponly=True, samesite="strict")
        return response

    @app.get("/api/state")
    def state():
        return snapshot()

    @app.post("/api/action")
    async def action(request: Request):
        raw_body = await request.body()
        if len(raw_body) > 64_000:
            raise HTTPException(413, "Trial settings are too large.")
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(400, "Expected trial settings as JSON.") from exc
        if not isinstance(body, dict) or not isinstance(body.get("action"), str):
            raise HTTPException(422, "Choose an application action.")
        name = body.pop("action")
        try:
            result = await asyncio.to_thread(acquisition.action, name, body)
            snapshot()  # Register output roots selected through the application.
            return result
        except (ValueError, RuntimeError, OSError) as exc:
            raise HTTPException(409, str(exc)) from exc

    def trial_files(trial_id: str) -> list[dict]:
        current = snapshot()
        active = current.get("trial") or {}
        if (str(active.get("id")) == trial_id
                and current.get("state") in {"ARMED", "RECORDING", "STOPPING"}):
            raise HTTPException(409, "Stop and save the trial before exporting it.")
        for root in tuple(known_roots):
            for trial in discover_trials(root):
                if str(trial.get("id")) == trial_id:
                    files = []
                    for item in trial.get("files", []):
                        path = Path(item["path"]).resolve()
                        if path.parent != root or not path.is_file():
                            continue
                        files.append({**item, "path": path})
                    return files
        raise HTTPException(404, "This trial is not in the selected data folders.")

    @app.get("/api/trials/{trial_id}/files/{kind}")
    def download(trial_id: str, kind: str):
        item = next((entry for entry in trial_files(trial_id) if entry["kind"] == kind), None)
        if item is None:
            raise HTTPException(404, "This trial file is unavailable.")
        return FileResponse(item["path"], filename=item["name"],
                            media_type="application/octet-stream")

    @app.get("/api/trials/{trial_id}/bundle")
    def bundle(trial_id: str):
        entries = trial_files(trial_id)
        stream = tempfile.SpooledTemporaryFile(max_size=4 * 1024 * 1024)
        try:
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for entry in entries:
                    archive.write(entry["path"], arcname=entry["name"])
                simulated = any(entry["kind"] == "simulation" for entry in entries)
                archive.writestr("SIMULATED_DATA.txt" if simulated else "ACQUISITION_NOTICE.txt",
                    "This bundle contains simulated flight-mill data.\n" if simulated else
                    "Consult the acquisition mode in the journal. Missing provenance means unknown, "
                    "not hardware. Keep the raw CSV with metadata, journal and protocol evidence.\n")
            stream.seek(0)
        except Exception:
            stream.close()
            raise

        def chunks():
            try:
                while chunk := stream.read(64 * 1024):
                    yield chunk
            finally:
                stream.close()

        return StreamingResponse(chunks(), media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="flightmill-{trial_id}.zip"',
        })

    @app.websocket("/ws")
    async def websocket(websocket: WebSocket):
        supplied = websocket.query_params.get("token", "")
        if not local_request(websocket.headers) or not secrets.compare_digest(supplied, token):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            while True:
                await websocket.send_json(await asyncio.to_thread(snapshot))
                await asyncio.sleep(0.2)
        except (WebSocketDisconnect, RuntimeError, OSError):
            return

    app.mount("/static", StaticFiles(directory=str(UI_ROOT / "static"), check_dir=False),
              name="static")
    return app

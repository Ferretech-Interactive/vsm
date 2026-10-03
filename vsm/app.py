"""FastAPI application: API, security middleware and the built single-page UI."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from . import VsmError, __version__
from .api.auth import same_origin
from .api.routes import public, router
from .manager import Manager

STATIC = Path(__file__).parent / "static"
CSP = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https://*.steamstatic.com; connect-src 'self' ws: wss:; frame-ancestors 'none'"


@asynccontextmanager
async def lifespan(app: FastAPI):
    m: Manager = app.state.manager
    m.hub.loop = asyncio.get_running_loop()
    m.start()
    yield
    await asyncio.to_thread(m.shutdown)


def create_app() -> FastAPI:
    app = FastAPI(title="Vein Server Manager", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.manager = Manager()

    @app.middleware("http")
    async def security(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS") and not same_origin(request):
            return JSONResponse({"detail": "Cross-origin request refused."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = CSP
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(VsmError)
    async def vsm_error(_request: Request, exc: VsmError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    app.include_router(public)
    app.include_router(router)

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        if not (STATIC / "index.html").exists():
            return PlainTextResponse("The web UI has not been built. Run `npm install && npm run build` in ./web.", status_code=503)
        target = (STATIC / path).resolve()
        if path and target.is_file() and target.is_relative_to(STATIC.resolve()):
            return FileResponse(target)
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    return app

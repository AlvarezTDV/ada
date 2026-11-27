"""Compose and serve ADA's local HTTP adapter."""

import asyncio
from contextlib import asynccontextmanager
from functools import partial
import os
from pathlib import Path

from anyio import CapacityLimiter
from anyio.to_thread import run_sync
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ada_core.jobs import JobManager
from ada_core.session_manager import SessionManager
from .auth import COOKIE_TTL, TokenAuth
from .routes_chat import router
from .routes_jobs import router as jobs_router
from .schemas import Login


SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
STATIC_DIR = Path(__file__).resolve().parent / "static"
PUBLIC_ROUTES = {("GET", "/health"), ("POST", "/auth/login")} | {
    ("GET", path) for path in ("/", "/manifest.webmanifest", "/sw.js",
                               "/static/app.js", "/static/styles.css", "/static/icon.svg")
}


def _runtime():
    from ada import create_session
    from ada_adapters.conversation_sqlite import SqliteConversationStore

    session_factory = partial(create_session, provider=None, headless=True)
    return SessionManager(session_factory, SqliteConversationStore()), session_factory().status


def create_app(session_manager=None, status=None, *, token=None,
               cookie_secure=True, cookie_ttl=COOKIE_TTL) -> FastAPI:
    auth = TokenAuth(os.getenv("ADA_TOKEN", "") if token is None else token,
                     cookie_secure=cookie_secure, cookie_ttl=cookie_ttl)
    if (session_manager is None) != (status is None):
        raise ValueError("Inject both the session manager and the status callable")

    @asynccontextmanager
    async def lifespan(app):
        manager, read_status = await run_sync(_runtime) if session_manager is None else (session_manager, status)
        app.state.manager, app.state.status = manager, read_status
        app.state.jobs = JobManager()
        # Idle streams must not exhaust workers serving chat or cancellation.
        app.state.sse_limiter = CapacityLimiter(16)
        try:
            yield
        finally:
            try:
                await run_sync(app.state.jobs.shutdown)
            finally:
                await run_sync(manager.shutdown_all)

    app = FastAPI(lifespan=lifespan, openapi_url=None, docs_url=None, redoc_url=None)
    app.state.auth = auth

    @app.middleware("http")
    async def secure(request: Request, call_next):
        if "token" in request.query_params:
            response = JSONResponse({"detail": "Query credentials are not allowed"}, status_code=400)
        elif (request.method, request.scope["path"]) not in PUBLIC_ROUTES and not auth.authorized(request):
            error = auth.unauthorized()
            response = JSONResponse({"detail": error.detail}, status_code=error.status_code, headers=error.headers)
        else:
            response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        if request.scope["path"] == "/sw.js" and 200 <= response.status_code < 300:
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        return JSONResponse({"detail": "Invalid request body"}, status_code=422)

    @app.exception_handler(Exception)
    async def internal_error(request, error):
        return JSONResponse({"detail": "Internal server error"}, status_code=500, headers=SECURITY_HEADERS)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    @app.get("/manifest.webmanifest")
    async def manifest():
        return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/sw.js")
    async def worker():
        return FileResponse(STATIC_DIR / "sw.js", media_type="text/javascript",
                            headers={"Service-Worker-Allowed": "/"})

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/auth/login")
    async def login(body: Login, response: Response):
        auth.login(response, body.token.get_secret_value())
        return {"authenticated": True}

    @app.post("/auth/logout", status_code=204)
    async def logout():
        response = Response(status_code=204)
        auth.logout(response)
        return response

    @app.get("/auth/me")
    async def me():
        return {"authenticated": True}

    app.include_router(router)
    app.include_router(jobs_router)
    return app


def main():
    from uvicorn import Config, Server
    from uvicorn.main import STARTUP_FAILURE

    cert, key = os.getenv("ADA_TLS_CERTFILE"), os.getenv("ADA_TLS_KEYFILE")
    if bool(cert) != bool(key):
        raise ValueError("Set both ADA_TLS_CERTFILE and ADA_TLS_KEYFILE to enable HTTPS")
    app = create_app()

    class JobServer(Server):
        async def shutdown(self, sockets=None):
            # Lifespan teardown runs after the HTTP drain. Start cancelling
            # now so a live SSE response cannot keep its own job running.
            stop_jobs = asyncio.create_task(asyncio.to_thread(app.state.jobs.shutdown))
            try:
                await super().shutdown(sockets=sockets)
            finally:
                await stop_jobs

    server = JobServer(Config(app, host="127.0.0.1", port=8000, access_log=False, proxy_headers=False,
                              ssl_certfile=cert, ssl_keyfile=key))
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    if not server.started:
        raise SystemExit(STARTUP_FAILURE)


if __name__ == "__main__":
    main()

"""Public site: landing page, demo, pilot request.

Registered onto the same FastAPI app as the product API so there is one process,
one deployment and one repository. The split is by audience, not by technology:

  /            marketing — no database, no snapshot, pure static
  /demo        the curated snapshot; works with no database at all
  /pilot       the only route that writes anything
  /app         the live product over the real database (off when DEMO_ONLY)

The live API stays exactly where it was so existing tooling and the README's
uvicorn command keep working.
"""
from __future__ import annotations

import pathlib

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import demo as demo_snapshot
from . import pilot as pilot_module
from .settings import demo_only, site_config

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"

# Long enough that a repeat visit is instant, short enough that a redeploy is
# visible within a day. The page images are content-addressed by document and
# page and never change without a re-export.
PAGE_CACHE = "public, max-age=86400"


def _page(name: str) -> FileResponse:
    path = STATIC / name
    if not path.exists():
        raise HTTPException(404, f"{name} not found")
    return FileResponse(path, media_type="text/html",
                        headers={"Cache-Control": "no-cache"})


def client_ip(request: Request) -> str | None:
    """The caller's address, honouring one proxy hop.

    Used only to derive a salted hash for rate limiting; it is never stored.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def register(app) -> None:
    # --- pages --------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def landing():
        return _page("index.html")

    @app.get("/demo", include_in_schema=False)
    def demo_page():
        return _page("demo.html")

    @app.get("/pilot", include_in_schema=False)
    def pilot_page():
        return _page("pilot.html")

    @app.get("/app", include_in_schema=False)
    def product_page():
        if demo_only():
            raise HTTPException(
                404, "the live product is not served by this deployment; see /demo")
        return _page("app.html")

    @app.get("/robots.txt", include_in_schema=False)
    def robots():
        return FileResponse(STATIC / "robots.txt", media_type="text/plain")

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        # Liveness only. The database is deliberately not checked: /demo works
        # without it, and a storage outage should surface as 503s from the
        # pilot form (which is monitored), not as the whole site being pulled.
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-cache"})

    # --- configuration ------------------------------------------------------

    @app.get("/api/config")
    def config():
        """Public site configuration. Contains nothing secret by construction."""
        return {**site_config(), "demo_only": demo_only()}

    # --- the demo, from the snapshot ---------------------------------------

    @app.get("/api/demo/overview")
    def demo_overview():
        try:
            return demo_snapshot.overview()
        except demo_snapshot.SnapshotMissing as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.get("/api/demo/tour")
    def demo_tour():
        try:
            return demo_snapshot.tour()
        except demo_snapshot.SnapshotMissing as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.get("/api/demo/claim/{claim_id}")
    def demo_claim(claim_id: int):
        try:
            found = demo_snapshot.claim(claim_id)
        except demo_snapshot.SnapshotMissing as exc:
            raise HTTPException(503, str(exc)) from exc
        if not found:
            # Not in the snapshot means not curated for the demo. Saying so is
            # more useful than 404 "no such claim", which implies it might exist.
            raise HTTPException(404, "not part of the demo snapshot")
        return found

    # --- the pilot request --------------------------------------------------

    @app.post("/api/pilot")
    async def submit_pilot(request: Request):
        """Store a pilot request. Success is returned only once it is stored."""
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "Send JSON."}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "Send a JSON object."}, status_code=400)

        try:
            parsed = pilot_module.parse(payload)
        except pilot_module.ValidationFailed as exc:
            return JSONResponse({"error": "Please check the highlighted fields.",
                                 "fields": exc.errors}, status_code=422)

        try:
            result = pilot_module.store(parsed, client_ip=client_ip(request))
        except pilot_module.RateLimited as exc:
            return JSONResponse({"error": str(exc)}, status_code=429)
        except pilot_module.StorageUnavailable:
            # Deliberately not "thanks!": nothing was written, and the visitor
            # needs a way to reach us that does not depend on our database.
            return JSONResponse(
                {"error": "We could not store your request just now. "
                          f"Please email {site_config()['contact_email']} and we will "
                          "pick it up from there.",
                 "fallback_email": site_config()["contact_email"]},
                status_code=503,
            )

        pilot_module.forward_to_crm(parsed, result["id"])
        return {
            "stored": True,
            "duplicate": result["duplicate"],
            "message": ("Thanks — we already had a request from this address, so we have "
                        "updated it." if result["duplicate"] else
                        "Thanks — your request is stored. We reply to every scoped "
                        "request, usually within two working days."),
            "work_email": parsed.is_work_address,
        }

    # --- assets -------------------------------------------------------------

    app.mount("/demo/pages", StaticFiles(directory=STATIC / "demo" / "pages"),
              name="demo-pages")
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

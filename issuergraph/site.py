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

import hashlib
import html
import pathlib
from datetime import date
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import demo as demo_snapshot
from . import pilot as pilot_module
from .settings import crm_webhook, demo_only, site_config, site_url, trusted_proxy_hops

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"

# Long enough that a repeat visit is instant, short enough that a redeploy is
# visible within a day. The page images are content-addressed by document and
# page and never change without a re-export.
PAGE_CACHE = "public, max-age=86400"


def asset_version() -> str:
    """A short hash of every script and stylesheet the pages load.

    Pages reference /static/app.js?v=<this>, so a deploy that changes any of
    them changes the URL, and the scripts themselves are served no-cache. A
    browser holding last release's app.js against this release's snapshot
    rendered the new data with the old code.
    """
    digest = hashlib.sha256()
    for path in sorted(STATIC.glob("*.js")) + sorted(STATIC.glob("*.css")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


ASSET_VERSION = asset_version()

# Every analytics event the site can send, as the privacy notice lists them.
# tests/test_site_polish.py checks this against the track() calls in static/.
ANALYTICS_EVENTS = {
    "landing_viewed": "the home page was opened",
    "demo_started": "the demo was opened",
    "demo_tab_viewed": "which demo tab was opened",
    "sources_compared": "the demo's Differences tab was opened",
    "evidence_opened": "a piece of evidence was opened (its type, publisher and page number)",
    "tour_started": "the guided tour was started",
    "tour_ended": "the guided tour was finished or closed, and at which step",
    "demo_cta_clicked": "a link to the demo was clicked",
    "pilot_cta_clicked": "a link to the pilot form was clicked",
    "booking_cta_clicked": "the booking link was clicked",
    "email_cta_clicked": "the email link was clicked",
    "pilot_form_viewed": "the pilot form was opened",
    "pilot_submitted": "the pilot form was submitted (not what was typed into it)",
    "pilot_submit_failed": "a submission failed, and which fields were rejected (their "
                           "names, not their contents)",
}


def _page(name: str) -> HTMLResponse:
    """A page with its {{tokens}} filled in on the server.

    The browser still updates the same values from /api/config and the demo
    snapshot, but a crawler or a link preview reads the HTML as sent — before
    any script runs — so the founder, the counts and the dataset cutoff are in
    it already rather than "—".
    """
    path = STATIC / name
    if not path.exists():
        raise HTTPException(404, f"{name} not found")
    body = path.read_text()
    for key, value in page_values().items():
        body = body.replace("{{" + key + "}}", value)
    return HTMLResponse(body, headers={"Cache-Control": "no-cache"})


def _day(value) -> str:
    if not value:
        return ""
    d = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    return f"{d.day:02d} {d:%b %Y}"


def page_values() -> dict[str, str]:
    """Every {{token}} a page may use, HTML-escaped (raw HTML only where built here)."""
    cfg = site_config()
    esc = lambda v: html.escape(str(v or ""), quote=True)
    try:
        overview = demo_snapshot.overview()
        counts, cutoff = overview["counts"], overview["document_cutoff"]
    except Exception:                    # no snapshot in this environment
        counts, cutoff = {}, None
    crm_url, _ = crm_webhook()
    linkedin = cfg.get("founder_linkedin")
    bio = cfg.get("founder_bio")
    return {
        "site_url": esc(site_url()),
        "product_name": esc(cfg["product_name"]),
        "founder_name": esc(cfg["founder_name"]),
        "founder_role": esc(cfg["founder_role"]),
        "founder_bio_block": (f'<p id="founder-bio" data-cfg="founder_bio">{esc(bio)}</p>'
                              if bio else ""),
        # With no profile configured there is no link at all, not a dead href="#".
        "founder_linkedin_link": (f'<a id="founder-linkedin" href="{esc(linkedin)}" '
                                  f'rel="me noopener" target="_blank">LinkedIn</a>'
                                  if linkedin else ""),
        "contact_email": esc(cfg["contact_email"]),
        "booking_link": (f'<a class="btn" id="booking-link" href="{esc(cfg["booking_url"])}" '
                         f'data-cta="booking">Book a call</a>' if cfg.get("booking_url") else ""),
        "count_documents": esc(counts.get("documents", "several")),
        "count_facts": esc(counts.get("claims", "")),
        "count_open_differences": esc(counts.get("conflicts_open", "")),
        "count_resolved": esc(counts.get("conflicts_resolved", "")),
        "count_changes": esc(counts.get("changes", "")),
        "cutoff": esc(_day(cutoff) or "its stated cutoff"),
        "asset_version": ASSET_VERSION,
        "analytics_events": "".join(f"<li><code>{esc(name)}</code> — {esc(what)}</li>"
                                    for name, what in ANALYTICS_EVENTS.items()),
        # The privacy notice states what this deployment actually does.
        "privacy_crm": ("Your request is also sent to the system we use to manage replies, "
                        "with the same fields." if crm_url else
                        "It is not forwarded to any other system."),
        "privacy_analytics": (
            "Analytics are enabled on this site. We use PostHog to count the events "
            "listed below. Events carry "
            "no name, email address, organisation, typed text or document content; "
            "automatic capture, session recording and personal profiles are switched "
            "off. PostHog keeps an anonymous identifier in your browser so repeat visits "
            "can be counted." if cfg["analytics"]["enabled"] else
            "Analytics are switched off on this site: no analytics script is loaded, "
            "no event is sent and nothing is stored in your browser for analytics. "
            "If we switch them on, this notice will say so, and the events below are "
            "the only ones that could be sent."),
    }


def canonical_redirect(request: Request) -> RedirectResponse | None:
    """www.<site> answers with a permanent redirect to the apex, path kept."""
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "")
    host = host.split(",")[0].strip().lower()
    apex = urlsplit(site_url()).hostname or ""
    if not host.startswith("www.") or host.split(":")[0][4:] != apex:
        return None
    query = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(f"{site_url()}{request.url.path}{query}", status_code=301)


def client_ip(request: Request) -> str | None:
    """The caller's address, as established by the proxies we actually trust.

    Used only to derive a salted hash for rate limiting; it is never stored.

    Reading the first X-Forwarded-For value trusted whoever wrote it: a caller
    could send a fresh header per request and never be throttled, and could
    still do so through a proxy that appends rather than replaces. Each trusted
    proxy appends exactly one address, so with N trusted hops in front the
    caller is the Nth value from the right — everything to its left is
    whatever the caller chose to say. With no trusted hops the peer address is
    the caller and the header is ignored entirely.
    """
    hops = trusted_proxy_hops()
    peer = request.client.host if request.client else None
    if hops == 0:
        return peer
    chain = [v.strip() for v in request.headers.get("x-forwarded-for", "").split(",")
             if v.strip()]
    if len(chain) < hops:
        # Fewer hops than configured means the request did not come through
        # the proxies we trust; the peer is the best available identity.
        return peer
    return chain[-hops]


# The public pages a crawler should know about; /app is the product, not the site.
SITEMAP = ("/", "/demo", "/pilot", "/privacy")

# Everything a demo-only deployment answers under /api. Anything else there is
# the live database API and is refused, whatever the database holds.
PUBLIC_API = ("/api/config", "/api/demo/", "/api/pilot")


def public_path(path: str) -> bool:
    """Is this path served when the live product is switched off?"""
    if not path.startswith("/api/"):
        return True
    return any(path == p or path.startswith(p) for p in PUBLIC_API)


def register(app) -> None:
    # --- demo-only gate -----------------------------------------------------

    @app.middleware("http")
    async def revalidate_assets(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/") and request.url.path.endswith((".js", ".css")):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.middleware("http")
    async def apex_only(request: Request, call_next):
        redirect = canonical_redirect(request)
        return redirect or await call_next(request)

    @app.middleware("http")
    async def gate_live_api(request: Request, call_next):
        # Hiding /app was not enough: the JSON routes behind it stayed
        # reachable, so a public host pointed at a workspace database would have
        # served its contents to anyone who guessed the URL. The gate is by
        # path prefix, so a live route added later is closed by default.
        if demo_only() and not public_path(request.url.path):
            return JSONResponse(
                {"detail": "the live API is not served by this deployment; see /demo"},
                status_code=404)
        return await call_next(request)

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

    @app.get("/privacy", include_in_schema=False)
    def privacy_page():
        return _page("privacy.html")

    @app.get("/app", include_in_schema=False)
    def product_page():
        if demo_only():
            raise HTTPException(
                404, "the live product is not served by this deployment; see /demo")
        return _page("app.html")

    @app.get("/robots.txt", include_in_schema=False)
    def robots():
        body = (STATIC / "robots.txt").read_text().rstrip("\n")
        return Response(f"{body}\nSitemap: {site_url()}/sitemap.xml\n",
                        media_type="text/plain")

    @app.get("/sitemap.xml", include_in_schema=False)
    def sitemap():
        urls = "".join(f"<url><loc>{html.escape(site_url() + path)}</loc></url>"
                       for path in SITEMAP)
        return Response('<?xml version="1.0" encoding="UTF-8"?>'
                        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                        f"{urls}</urlset>", media_type="application/xml")

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

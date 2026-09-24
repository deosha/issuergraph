"""Deployment configuration, read from the environment.

Everything a deployment legitimately varies — who to contact, whether analytics
is on, what a pilot costs — lives here rather than in the markup, so the site
can be rebranded or re-priced without editing HTML. Nothing here has a secret as
its default, and `site_config()` is explicit about what may reach the browser.

Pricing defaults to empty on purpose: the landing page then asks for scope
rather than quoting a number, which is the honest default for a pilot that has
not been scoped.
"""
from __future__ import annotations

import os
from functools import lru_cache


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _flag(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@lru_cache(maxsize=1)
def site_config() -> dict:
    """The public site configuration served to the browser.

    Public by construction: this dict is returned by /api/config, so nothing
    secret may be added to it. Server-side secrets (CRM tokens) are read
    separately and never pass through here.
    """
    return {
        "product_name": _env("ISSUERGRAPH_PRODUCT_NAME", "IssuerGraph"),
        "tagline": _env(
            "ISSUERGRAPH_TAGLINE",
            "Issuer debt and rating changes, with the source behind every figure.",
        ),
        "founder_name": _env("ISSUERGRAPH_FOUNDER_NAME", "Deo Shankar"),
        "founder_role": _env("ISSUERGRAPH_FOUNDER_ROLE", "Founder"),
        "founder_bio": _env("ISSUERGRAPH_FOUNDER_BIO", ""),
        "founder_linkedin": _env("ISSUERGRAPH_FOUNDER_LINKEDIN"),   # optional
        "contact_email": _env("ISSUERGRAPH_CONTACT_EMAIL", "hello@issuergraph.com"),
        "booking_url": _env("ISSUERGRAPH_BOOKING_URL"),      # optional
        "pilot_price": _env("ISSUERGRAPH_PILOT_PRICE"),      # empty = ask for scope
        "pilot_length": _env("ISSUERGRAPH_PILOT_LENGTH", "four weeks"),
        "analytics": {
            "enabled": bool(_env("POSTHOG_PROJECT_KEY")),
            "key": _env("POSTHOG_PROJECT_KEY"),
            "host": _env("POSTHOG_HOST", "https://eu.i.posthog.com"),
        },
    }


def crm_webhook() -> tuple[str, str]:
    """(url, bearer token) for optional server-side CRM forwarding."""
    return _env("ISSUERGRAPH_CRM_WEBHOOK_URL"), _env("ISSUERGRAPH_CRM_WEBHOOK_TOKEN")


def demo_only() -> bool:
    """Serve the demo without exposing the live database endpoints.

    A public deployment sets this: the marketing site and the snapshot-backed
    demo need no database at all, so there is no reason for the live API to be
    reachable from the internet.
    """
    return _flag("ISSUERGRAPH_DEMO_ONLY", False)


def trusted_proxy_hops() -> int:
    """How many proxies in front of this process append to X-Forwarded-For.

    0 (the default) ignores the header and uses the peer address. Behind App
    Runner or one load balancer it is 1. Set it to the number of hops you
    control, never to "trust everything": the rate limiter's identity is only
    as good as this number.
    """
    return max(0, int(_env("ISSUERGRAPH_TRUSTED_PROXY_HOPS", "0")))


def rate_limit() -> tuple[int, int]:
    """(max submissions, window in seconds) per client for the pilot form."""
    return (int(_env("ISSUERGRAPH_PILOT_RATE_MAX", "5")),
            int(_env("ISSUERGRAPH_PILOT_RATE_WINDOW", "3600")))


# How each publisher's evidence may be shown. 'page_image' renders the page the
# anchor sits on with the span highlighted. 'quote_and_link' serves no copy of
# the source at all — a short quote, the anchor's metadata, and a link to the
# publisher's own page with a text fragment — for publishers whose terms
# restrict redistribution (CRISIL). Anything not listed is 'page_image'.
EVIDENCE_POLICIES = ("page_image", "quote_and_link")
DEFAULT_EVIDENCE_POLICY = {"CRISIL": "quote_and_link"}


@lru_cache(maxsize=1)
def evidence_policies() -> dict[str, str]:
    """Publisher → policy. ISSUERGRAPH_EVIDENCE_POLICY adds or overrides
    entries as "CRISIL=quote_and_link,ICRA=page_image"; an unknown policy name
    is an error, never a silent fallback to showing the page."""
    policies = dict(DEFAULT_EVIDENCE_POLICY)
    for item in filter(None, (p.strip() for p in _env("ISSUERGRAPH_EVIDENCE_POLICY").split(","))):
        publisher, _, policy = item.partition("=")
        if policy.strip() not in EVIDENCE_POLICIES:
            raise ValueError(f"unknown evidence policy {policy!r} for {publisher!r}")
        policies[publisher.strip()] = policy.strip()
    return policies


def evidence_policy(source_name: str | None) -> str:
    return evidence_policies().get(source_name or "", "page_image")


def site_url() -> str:
    """The public site's absolute base URL: canonical links, Open Graph URLs
    and the www → apex redirect are built from it."""
    return _env("ISSUERGRAPH_SITE_URL", "https://issuergraph.com").rstrip("/")

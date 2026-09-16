"""Pilot requests: validate, throttle, store, optionally forward.

Three rules shape this module.

*Success means stored.* The endpoint returns success only after the row is
committed. A form that says "thanks, we'll be in touch" when the database was
unreachable is worse than an error, because nobody ever finds out.

*A resubmission is the same lead.* People correct a typo and send again. The
email is the identity, and a second submission updates the existing row and
increments a counter rather than creating a second lead to reply to twice.

*Throttling does not require a visitor log.* The rate limiter keys on a salted
hash of the client address, which cannot be read back into an address, and rows
older than the window are deleted on write.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request

from pydantic import BaseModel, Field, field_validator

from .db import connect
from .settings import crm_webhook, rate_limit

# Deliberately permissive: the job is to catch a visitor's typo, not to
# adjudicate RFC 5321. Anything shaped like an address gets through, and a real
# reply is what actually verifies it.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$")

FREE_EMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "live.com", "icloud.com", "aol.com", "proton.me", "protonmail.com",
    "gmx.com", "mail.com", "yandex.com", "zoho.com",
})


class ValidationFailed(ValueError):
    """Field-level problems, shaped for display next to the inputs."""

    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


class RateLimited(RuntimeError):
    pass


class StorageUnavailable(RuntimeError):
    """The database could not be reached. The visitor must be told the truth."""


class PilotRequest(BaseModel):
    """What a pilot conversation needs in order to be scoped.

    `notes` is the only optional field, and the form marks it as such.
    """

    name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=5, max_length=200)
    organisation: str = Field(min_length=2, max_length=160)
    role: str = Field(min_length=2, max_length=120)
    issuers: str = Field(min_length=2, max_length=2000)
    workflow: str = Field(min_length=10, max_length=4000)
    notes: str | None = Field(default=None, max_length=4000)
    source: str | None = Field(default=None, max_length=60)

    @field_validator("name", "organisation", "role", "issuers", "workflow", "notes",
                     "source", mode="before")
    @classmethod
    def _tidy(cls, value):
        return " ".join(value.split()) if isinstance(value, str) else value

    @field_validator("email", mode="before")
    @classmethod
    def _tidy_email(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("email")
    @classmethod
    def _shaped_like_an_address(cls, value: str) -> str:
        if not EMAIL_RE.match(value):
            raise ValueError("Enter a valid email address.")
        return value

    @property
    def email_key(self) -> str:
        return self.email.casefold()

    @property
    def domain(self) -> str:
        return self.email_key.rsplit("@", 1)[-1]

    @property
    def is_work_address(self) -> bool:
        return self.domain not in FREE_EMAIL_DOMAINS


FIELD_LABELS = {
    "name": "Name", "email": "Work email", "organisation": "Organisation",
    "role": "Role", "issuers": "Issuers of interest", "workflow": "Workflow",
    "notes": "Anything else", "source": "Source",
}

FRIENDLY = {
    "string_too_short": "{label} is required.",
    "missing": "{label} is required.",
    "string_too_long": "{label} is too long.",
    "value_error": "{message}",
}


def parse(payload: dict) -> PilotRequest:
    """Validate, raising field-keyed errors the form can render in place."""
    from pydantic import ValidationError
    try:
        return PilotRequest.model_validate(payload)
    except ValidationError as exc:
        errors: dict[str, str] = {}
        for error in exc.errors():
            field = str(error["loc"][0]) if error["loc"] else "form"
            label = FIELD_LABELS.get(field, field)
            template = FRIENDLY.get(error["type"], "{label} is not valid.")
            message = error.get("msg", "").removeprefix("Value error, ")
            errors.setdefault(field, template.format(label=label, message=message))
        raise ValidationFailed(errors) from exc


def client_hash(client_ip: str | None) -> str:
    """A salted, non-reversible key for throttling.

    The salt defaults to the process's own identity rather than a constant, so
    an operator who sets nothing still does not accumulate hashes that are
    comparable across deployments.
    """
    salt = os.environ.get("ISSUERGRAPH_RATE_SALT", "issuergraph-local-salt")
    return hashlib.sha256(f"{salt}:{client_ip or 'unknown'}".encode()).hexdigest()


def check_rate_limit(conn, client_ip: str | None) -> None:
    max_submissions, window = rate_limit()
    key = client_hash(client_ip)
    conn.execute(
        "DELETE FROM pilot_submission_log WHERE created_at < now() - make_interval(secs => %s)",
        (window,),
    )
    recent = conn.execute(
        "SELECT count(*) AS n FROM pilot_submission_log "
        "WHERE client_hash = %s AND created_at > now() - make_interval(secs => %s)",
        (key, window),
    ).fetchone()["n"]
    if recent >= max_submissions:
        raise RateLimited(
            "Too many requests from this connection. Try again later, or email us."
        )
    conn.execute("INSERT INTO pilot_submission_log (client_hash) VALUES (%s)", (key,))


def store(request: PilotRequest, client_ip: str | None = None) -> dict:
    """Persist the request. Returns {id, duplicate, submissions}.

    Raises StorageUnavailable rather than swallowing a database failure, so the
    caller cannot report success for something that was never written.
    """
    try:
        with connect() as conn:
            check_rate_limit(conn, client_ip)
            row = conn.execute(
                """
                INSERT INTO pilot_request (name, email, email_key, organisation, role,
                                           issuers, workflow, notes, source)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (email_key) DO UPDATE
                    SET name         = EXCLUDED.name,
                        email        = EXCLUDED.email,
                        organisation = EXCLUDED.organisation,
                        role         = EXCLUDED.role,
                        issuers      = EXCLUDED.issuers,
                        workflow     = EXCLUDED.workflow,
                        notes        = EXCLUDED.notes,
                        source       = EXCLUDED.source,
                        submissions  = pilot_request.submissions + 1,
                        updated_at   = now()
                RETURNING id, submissions
                """,
                (request.name, request.email, request.email_key, request.organisation,
                 request.role, request.issuers, request.workflow, request.notes,
                 request.source),
            ).fetchone()
            conn.commit()
    except (RateLimited, ValidationFailed):
        raise
    except Exception as exc:                      # psycopg errors, DSN problems
        raise StorageUnavailable(str(exc)) from exc

    return {"id": row["id"], "submissions": row["submissions"],
            "duplicate": row["submissions"] > 1}


def forward_to_crm(request: PilotRequest, stored_id: int) -> bool:
    """Optional, server-side, and never a reason to fail the submission.

    The visitor's success depends on our database, not on a third party being
    up. A forwarding failure is recorded by leaving crm_forwarded false.
    """
    url, token = crm_webhook()
    if not url:
        return False

    body = json.dumps({
        "id": stored_id, "name": request.name, "email": request.email,
        "organisation": request.organisation, "role": request.role,
        "issuers": request.issuers, "workflow": request.workflow,
        "notes": request.notes, "source": request.source,
    }).encode()
    http = urllib.request.Request(url, data=body, method="POST",
                                  headers={"Content-Type": "application/json"})
    if token:
        http.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(http, timeout=5) as response:
            ok = 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False

    if ok:
        try:
            with connect() as conn:
                conn.execute("UPDATE pilot_request SET crm_forwarded = TRUE WHERE id = %s",
                             (stored_id,))
                conn.commit()
        except Exception:
            pass          # the lead is stored; the flag is bookkeeping
    return ok

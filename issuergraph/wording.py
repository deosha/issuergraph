"""How stored rating fields read to a person.

Storage keeps each publisher's own wording and a normalized action list
("assigned; downgraded; removed"). Neither is a sentence. These helpers turn
them into one without changing what is stored or compared.
"""
from __future__ import annotations

import re
from datetime import date

CLASS_NAME = {
    "ncd": "debentures", "subordinated_debt": "subordinated debt",
    "perpetual_debt": "perpetual debt", "bank_facility": "bank facilities",
    "mld": "market-linked debentures", "commercial_paper": "commercial paper",
    "debt_securities": "debt securities",
}
GENERIC = re.compile(r"^(long|short)[ -]term instruments?$", re.I)
REPEAT = re.compile(r"\b(\w+(?:[ -]\w+)*)(?:\s+\1\b)+", re.I)


def display_name(instrument: str, instrument_class: str | None = None) -> str:
    """"Long Term Long Term Instruments" → "Long Term Instruments (subordinated debt)".

    CARE prints the term column into the instrument cell, doubling it; and
    its generic "Long Term Instruments" says nothing about what they are, so
    the class the annexure gave them is added.
    """
    name = " ".join(REPEAT.sub(r"\1", " ".join(instrument.split())).split())
    if GENERIC.match(name) and instrument_class in CLASS_NAME:
        name = f"{name} ({CLASS_NAME[instrument_class]})"
    return name


def action_text(action: str | None, rating: str | None, outlook: str | None,
                watch: str | None, previous: str | None) -> str:
    """"assigned; downgraded; removed" + AA-/Stable from AA →
    "downgraded to AA- from AA, removed from watch, Stable outlook assigned"."""
    verbs = [v.strip() for v in (action or "").split(";") if v.strip()]
    graded = any(v in ("downgraded", "upgraded") for v in verbs)
    parts = []
    for v in ("downgraded", "upgraded", "reaffirmed", "assigned", "placed on", "revised",
              "removed", "withdrawn"):
        if v not in verbs:
            continue
        if v in ("downgraded", "upgraded"):
            parts.append(f"{v} to {rating}" + (f" from {previous}" if previous else ""))
        elif v == "reaffirmed" and rating:
            parts.append(f"reaffirmed at {rating}")
        elif v == "assigned" and graded:
            continue                         # read with the outlook, below
        elif v == "assigned" and rating:
            parts.append(f"assigned {rating}")
        elif v == "placed on" and watch:
            parts.append(f"placed on {watch.lower()}")
        elif v == "revised":
            parts.append("watch revised to " + re.sub(r"^watch with ", "", watch.lower()) if watch
                         else f"outlook revised to {outlook}" if outlook else "revised")
        elif v == "removed":
            parts.append("removed from watch")
        else:
            parts.append(v)
    if graded and "assigned" in verbs and outlook:
        parts.append(f"{outlook} outlook assigned")
    if not parts:
        return f"rated {rating}" + (f"/{outlook}" if outlook else "") if rating else "no action stated"
    if outlook and not graded and not any("outlook" in p for p in parts) and "withdrawn" not in verbs:
        parts[0] += f"/{outlook}"
    return ", ".join(parts)


# How each publisher writes its own name, where it differs from the key we store.
PUBLISHER_NAME = {"CRISIL": "Crisil"}
ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def publisher(name: str | None) -> str | None:
    return PUBLISHER_NAME.get(name, name)


def readable(text: str | None) -> str | None:
    """Our own sentences (difference headings and notes, fact subjects) as a
    reader sees them: "15 Sep 2025", not "2025-09-15"; "Crisil", not the stored
    key. Never applied to a quote — a publisher's words stay as printed."""
    if not text:
        return text
    text = ISO_DATE.sub(lambda m: f"{date(*map(int, m.groups())):%d %b %Y}", text)
    for key, name in PUBLISHER_NAME.items():
        text = re.sub(rf"\b{key}\b", name, text)
    return text

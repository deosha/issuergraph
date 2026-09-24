"""Crisil Ratings rationale extractor (HTML).

Crisil publishes rationales as Word-exported HTML: one layout table holding a
nested table per section. Tables are rebuilt from the units' node paths
(`.../table[2]/tbody/tr[4]/td[1]`), so a cell's row and column come from where
it is, not from counting non-empty cells — an empty cell can never shift a
column. Every anchor is a <td>, <p> or <li> unit via `anchor_in`, with raw
offsets; matching happens on collapsed whitespace and is mapped back.

Footnote markers are read per table, never globally: the rating-action table
uses & for "interchangeable between secured and subordinated" and ^ for
"retail", while the instrument annexure uses # for "interchangeable", & for
"retail", ** for "not yet issued" and * for "interchangeable with short-term
bank loan facility". The same symbol means different things a few rows apart.

Grades are normalised for comparison — the "Crisil" prefix dropped, "PPMLD AA"
read as AA with a 'ppmld' qualifier, the legacy " r" suffix read as the same
grade with a 'legacy_r' qualifier, "Watch Developing" as the watch wording the
other agencies use — while each claim keeps the publisher's own string.

Key financial indicators are "Crisil Ratings-adjusted": they carry basis
'agency_adjusted' and are never compared with reported figures (reconcile.py).
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from ..models import DebtDetail, ExtractedClaim, HistoryDetail, RatingDetail
from .common import (anchor_in, find_published_date, normalized_view, parse_action,
                     quarter_key, raw_span, rating_identity)
from .coverage import Coverage, rating_rationale_coverage

EXTRACTOR = "crisil_rationale"
VERSION = "1.3.0"
AGENCY = "CRISIL"

CELL = re.compile(r"^(?P<table>.+/table(?:\[\d+\])?)/(?:tbody/)?tr(?:\[(?P<row>\d+)\])?"
                  r"/td(?:\[(?P<col>\d+)\])?$")
RATING = re.compile(
    r"Crisil\s+(?P<ppmld>PPMLD\s+)?(?P<grade>A[1-4]\+?|AAA|AA|A|BBB|BB|B|C|D)"
    r"(?P<mod>[+-])?(?P<legacy>\s+r\b)?\s*"
    r"(?:/\s*(?P<view>Stable|Negative|Positive|Watch\s+(?:Developing|Negative|Positive)))?"
    r"(?:\s*\((?P<action>[^)]*)\))?",
    re.IGNORECASE)
AMOUNT = re.compile(r"Rs\.?\s*(?P<n>[\d,]+(?:\.\d+)?)\s*Crore", re.IGNORECASE)
REDUCED = re.compile(r"\((?:Reduced|Enhanced|Increased)\s+from\s+Rs\.?\s*[\d,.]+\s*Crore\)",
                     re.IGNORECASE)
# Markers are read only from instrument names and footnote lines, so "%"
# (Secured, from 2026) cannot collide with a percentage in the prose.
MARKERS = re.compile(r"(\*\*|[&^#*%])")
FOOTNOTE = re.compile(r"(\*\*|[&^#*%])\s*([A-Za-z][^&^#*%]*)")
SHORT_DATE = re.compile(r"^\d{1,2}-\d{1,2}-\d{2}$")                   # history: 30-10-25
ANNEX_DATE = re.compile(r"^\d{1,2}-[A-Za-z]{3}-\d{2}$")               # annexure: 30-Jun-28
UNITS = {"rs crore": "INR_CRORE", "%": "PERCENT", "times": "TIMES"}
# "Outlook Stable" (2026) or "Outlook: Stable" (2024).
OUTLOOK_LINE = re.compile(r"^Outlook:?\s+(Stable|Negative|Positive|Developing)$", re.IGNORECASE)
LIQUIDITY_LINE = re.compile(r"^Liquidity:?\s+(Superior|Strong|Adequate|Stretched|Poor)$",
                            re.IGNORECASE)
# Crisil edits rationales in place at the same URL and says so at the foot.
UPDATED_ON = re.compile(r"This RR was updated on ([A-Z][a-z]+ \d{1,2}, \d{4})")
DRIVERS = {"Key Rating Drivers - Strengths": "strengths",
           "Key Rating Drivers - Weaknesses": "weaknesses"}
KFI_HEADING = re.compile(r"Key financial indicators.*\((consolidated|standalone);[^)]*adjusted",
                         re.IGNORECASE)


# --- reading the page -----------------------------------------------------------

def flat(unit: dict) -> str:
    return " ".join(unit["text"].split())


def grids(units: list[dict]) -> dict[str, dict[int, dict[int, dict]]]:
    """table path → row → column → <td> unit, in document order."""
    out: dict[str, dict[int, dict[int, dict]]] = {}
    for u in units:
        m = CELL.match(u["node_path"])
        if m:
            out.setdefault(m["table"], {}).setdefault(int(m["row"] or 1), {})[
                int(m["col"] or 1)] = u
    return out


def whole(unit: dict):
    return anchor_in(unit, 0, len(unit["text"]))


def span(unit: dict, pattern: re.Pattern, group: int | str = 0):
    """Anchor the first match of `pattern` (run on collapsed whitespace) in raw offsets."""
    norm, index = normalized_view(unit["text"])
    m = pattern.search(norm)
    if not m:
        return None
    return anchor_in(unit, *raw_span(index, m.start(group), m.end(group)))


def read_rating(text: str) -> dict:
    """Grade, outlook, watch, action and qualifiers from a Crisil rating string."""
    compact = " ".join(text.split())
    if compact.casefold() == "withdrawn":
        return {"grade": None, "outlook": None, "watch": None, "action": "withdrawn",
                "withdrawn": True, "qualifiers": []}
    m = RATING.search(compact)
    if not m:
        return {"grade": None, "outlook": None, "watch": None, "action": None,
                "withdrawn": False, "qualifiers": []}
    view = m["view"]
    outlook = watch = None
    if view and view.lower().startswith("watch"):
        watch = f"Watch with {view.split()[-1].capitalize()} Implications"
    elif view:
        outlook = view.capitalize()
    action = None
    if m["action"]:
        action = parse_action(m["action"]) or m["action"].strip().lower()
    return {"grade": m["grade"].upper() + (m["mod"] or ""), "outlook": outlook, "watch": watch,
            "action": action, "withdrawn": bool(action and "withdrawn" in action),
            "qualifiers": [q for q, on in (("ppmld", m["ppmld"]), ("legacy_r", m["legacy"]))
                           if on]}


def number(text: str) -> Decimal | None:
    """"81,342" → 81342; "(1.2)" → -1.2; "NA", "-", "" → None."""
    t = text.strip().replace(",", "")
    negative = t.startswith("(") and t.endswith(")")
    try:
        value = Decimal(t.strip("()"))
    except InvalidOperation:
        return None
    return -value if negative else value


def annex_date(text: str) -> date | None:
    t = text.strip()
    return datetime.strptime(t, "%d-%b-%y").date() if ANNEX_DATE.match(t) else None


def history_date(text: str) -> date | None:
    t = text.strip()
    return datetime.strptime(t, "%d-%m-%y").date() if SHORT_DATE.match(t) else None


def period_date(text: str) -> date | None:
    """"December 31, 2025" or "Mar 31, 2025/ FY25"."""
    head = text.split("/")[0].strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(head, fmt).date()
        except ValueError:
            pass
    return None


def footnotes(units: list[dict], after_path: str, limit: int = 8) -> tuple[dict, list[dict]]:
    """Marker → meaning from the footnote lines just after a table, and their units."""
    start = next((i for i, u in enumerate(units) if u["node_path"].startswith(after_path)), None)
    if start is None:
        return {}, []
    meanings, lines = {}, []
    tail = [u for u in units[start:] if not u["node_path"].startswith(after_path)][:limit]
    for u in tail:
        text = flat(u)
        if not MARKERS.match(text):
            if lines:
                break
            continue
        for marker, meaning in FOOTNOTE.findall(text):
            meanings[marker] = meaning.strip()
        lines.append(u)
    return meanings, lines


def qualifiers_from(markers: list[str], meanings: dict[str, str]) -> list[str]:
    out = []
    for marker in markers:
        meaning = meanings.get(marker, "").lower()
        if "secured and subordinated" in meaning:
            out.append("interchangeable_subordinated")
        elif meaning.strip(" .") == "secured":
            out.append("secured")
        elif "retail" in meaning:
            out.append("retail")
        elif "not yet issued" in meaning:
            out.append("not_yet_issued")
        elif "short-term bank loan" in meaning:
            out.append("interchangeable_short_term")
    return out


def instrument_name(text: str) -> tuple[str, list[str], Decimal | None]:
    """(clean name, footnote markers, rated amount) from an action-table cell."""
    t = " ".join(text.split())
    amount = AMOUNT.search(t)
    markers = MARKERS.findall(re.sub(r"\([^)]*\)", "", t))
    name = REDUCED.sub("", t)
    name = AMOUNT.sub("", name)
    name = re.sub(r"\bAggregating\b", "", name, flags=re.IGNORECASE)
    name = MARKERS.sub("", name)
    name = " ".join(name.split())
    value = Decimal(amount["n"].replace(",", "")) if amount else None
    return name, markers, value


# --- the tables -----------------------------------------------------------------

def _find(tables: dict, test) -> list[tuple[str, dict]]:
    return [(path, rows) for path, rows in tables.items() if test(rows)]


def _header(rows: dict) -> list[str]:
    first = rows[min(rows)]
    return [flat(first[c]) for c in sorted(first)]


def _is_action(rows: dict) -> bool:
    """Two columns, an amount in crore on every row, a Crisil rating on some.

    Recognised by shape, not by every rating parsing: a single unreadable
    rating cell must surface as that row's shortfall, not make the whole table
    unrecognisable and so silently absent.
    """
    return bool(rows) and all(set(r) == {1, 2} and "crore" in flat(r[1]).lower()
                              for r in rows.values()) and any(
        RATING.search(flat(r[2])) for r in rows.values())


def _is_bank(rows: dict) -> bool:
    texts = [flat(r[1]) for r in rows.values() if 1 in r]
    return any(t.startswith("Total Bank Loan Facilities") for t in texts) and any(
        t.startswith("Long Term Rating") or t.startswith("Short Term Rating") for t in texts)


def _is_annexure(rows: dict) -> bool:
    head = _header(rows)
    return len(head) >= 8 and head[0] == "ISIN" and head[1].startswith("Name of instrument")


def _is_history(rows: dict) -> bool:
    return any(flat(r.get(1, {"text": ""})) == "Instrument" and
               flat(r.get(3, {"text": ""})).startswith("Outstanding") for r in rows.values())


def _is_kfi(rows: dict) -> bool:
    return _header(rows)[0].replace(" ", "").lower().startswith("ason/forthe")


def _is_lenders(rows: dict) -> bool:
    return _header(rows)[:3] == ["Facility", "Amount (Rs.Crore)", "Name of Lender"]


def read(doc_meta, units: list[dict]) -> dict:
    """Every claim, plus what coverage needs to judge the result."""
    pub = getattr(doc_meta, "published_date", None) or next(
        (d for d in (find_published_date(flat(u)) for u in units[:40]) if d), None)
    qkey = quarter_key(pub)
    tables = grids(units)
    claims: list[ExtractedClaim] = []
    report = {"action_rows": [], "action_rated": [], "action_table": False,
              "annex_rows": [], "annex_read": [],
              "history_entries": 0, "unreadable": [], "sections": set()}

    def rating_claim(name, amount, cell_units, rating_unit, qualifiers, rated_note=""):
        r = read_rating(flat(rating_unit))
        if not (r["grade"] or r["withdrawn"]):
            report["unreadable"].append(f"rating {flat(rating_unit)!r} for {name!r}")
            return None
        instrument_class, term = rating_identity(name, r["grade"])
        anchors = [whole(u) for u in cell_units] + [whole(rating_unit)]
        slug = f"{name.lower()}|{amount}{rated_note}"
        claims.append(ExtractedClaim(
            claim_type="rating", fact_key=f"rating_instrument|{AGENCY}|{slug}|{qkey}",
            subject=f"{name} — {AGENCY} rating", value_text=flat(rating_unit),
            as_of_date=pub, extractor=EXTRACTOR, extractor_version=VERSION, anchors=anchors,
            rating=RatingDetail(agency=AGENCY, instrument=name, rated_amount_cr=amount,
                                rating=r["grade"], instrument_class=instrument_class, term=term,
                                outlook=r["outlook"], watch=r["watch"],
                                action=r["action"] or ("withdrawn" if r["withdrawn"] else None),
                                action_date=pub,
                                qualifiers=r["qualifiers"] + qualifiers)))
        if amount is not None:
            claims.append(ExtractedClaim(
                claim_type="debt_instrument",
                fact_key=f"rated_amount|{AGENCY}|{slug}|{qkey}",
                subject=f"{name} (rated amount)", value_numeric=amount, value_unit="INR_CRORE",
                as_of_date=pub, extractor=EXTRACTOR, extractor_version=VERSION,
                anchors=[whole(u) for u in cell_units],
                debt=DebtDetail(instrument_name=name, instrument_type=instrument_class,
                                amount_cr=amount, as_of_date=pub)))
        return claims[-1]

    # bank facilities: "Total Bank Loan Facilities Rated | Rs.7000 Crore" / "Long Term Rating | ..."
    for path, rows in _find(tables, _is_bank):
        total = next(r for r in rows.values() if flat(r[1]).startswith("Total Bank Loan"))
        rated = next(r for r in rows.values() if flat(r[1]).endswith("Rating"))
        report["action_rows"].append(total[1]["node_path"])
        amount = instrument_name(flat(total[2]))[2]
        if rating_claim("Bank Loan Facilities", amount, [total[1], total[2]], rated[2], []):
            report["action_rated"].append(total[1]["node_path"])

    # rating actions, with this table's own footnote markers
    for path, rows in _find(tables, _is_action):
        report["action_table"] = True
        meanings, _ = footnotes(units, path)
        for _, row in sorted(rows.items()):
            report["action_rows"].append(row[1]["node_path"])
            name, markers, amount = instrument_name(flat(row[1]))
            if rating_claim(name, amount, [row[1]], row[2], qualifiers_from(markers, meanings)):
                report["action_rated"].append(row[1]["node_path"])

    # annexures: instruments, and ratings withdrawn
    for path, rows in _find(tables, _is_annexure):
        meanings, _ = footnotes(units, path)
        body = [row for n, row in sorted(rows.items()) if n != min(rows)]
        withdrawn_table = bool(body) and all(flat(r[8]) == "Withdrawn" for r in body if 8 in r)
        for row in body:
            if not {1, 2, 5, 6, 8} <= set(row):
                report["unreadable"].append(f"annexure row {row.get(1, row.get(2))['node_path']}")
                continue
            raw_name = flat(row[2])
            markers = MARKERS.findall(raw_name)
            name = " ".join(MARKERS.sub("", raw_name).split())
            isin = flat(row[1])
            size = number(flat(row[6]))
            maturity = annex_date(flat(row[5]))
            quals = qualifiers_from(markers, meanings)
            if withdrawn_table:
                if rating_claim(name, None, [row[1], row[2]], row[8], quals,
                                rated_note=f"|{isin}|withdrawn"):
                    continue
                continue
            report["annex_rows"].append(row[2]["node_path"])
            if size is None:
                report["unreadable"].append(f"issue size {flat(row[6])!r} for {isin}")
                continue
            instrument_class = rating_identity(name, read_rating(flat(row[8]))["grade"])[0]
            coupon = flat(row[4]) if 4 in row else "NA"
            claims.append(ExtractedClaim(
                claim_type="debt_instrument",
                fact_key=f"instrument|{isin}|{maturity or flat(row[5])}|{size}",
                subject=f"{name} {isin} (coupon {coupon}; {flat(row[8])})",
                value_numeric=size, value_unit="INR_CRORE", as_of_date=pub,
                extractor=EXTRACTOR, extractor_version=VERSION,
                anchors=[whole(row[c]) for c in (1, 2, 5, 6, 8)],
                debt=DebtDetail(instrument_name=name, instrument_type=instrument_class,
                                amount_cr=size, maturity_date=maturity, as_of_date=pub)))
            report["annex_read"].append(row[2]["node_path"])
            # Crisil rates each subordinated NCD itself (decision A): that
            # rating is compared as subordinated debt.
            if instrument_class == "subordinated_debt":
                rating_claim(name, None, [row[1], row[2]], row[8], quals,
                             rated_note=f"|{isin}|{maturity or flat(row[5])}")

    # rating history: first row of a group names the instrument; (Date, Rating) pairs
    for path, rows in _find(tables, _is_history):
        head_no = next(n for n, r in sorted(rows.items()) if flat(r.get(1, {"text": ""})) ==
                       "Instrument")
        head = rows[head_no]
        pairs = [(c, c + 1) for c in sorted(head) if flat(head[c]) == "Date"]
        group = None
        for n, row in sorted(rows.items()):
            if n <= head_no:
                continue
            if 1 in row and flat(row[1]):
                group = row[1]
            if group is None:
                continue
            for dc, rc in pairs:
                if dc not in row or rc not in row:
                    continue
                when, rating_text = history_date(flat(row[dc])), flat(row[rc])
                if when is None or rating_text in ("--", "") or when == pub:
                    continue
                r = read_rating(rating_text)
                if not (r["grade"] or r["withdrawn"]):
                    report["unreadable"].append(f"history {flat(group)!r} {when}: {rating_text!r}")
                    continue
                name = flat(group)
                instrument_class, term = rating_identity(name, r["grade"])
                claims.append(ExtractedClaim(
                    claim_type="rating_history",
                    fact_key=(f"rating_history|{AGENCY}|{instrument_class}|{term}|"
                              f"{when.isoformat()}|{name.lower()}"),
                    subject=f"{name} — {AGENCY} rating history, {when.isoformat()}",
                    value_text=rating_text, as_of_date=when,
                    extractor=EXTRACTOR, extractor_version=VERSION,
                    anchors=[whole(group), whole(row[dc]), whole(row[rc])],
                    history=HistoryDetail(agency=AGENCY, instrument=name,
                                          instrument_class=instrument_class, term=term,
                                          action_date=when, grade=r["grade"],
                                          outlook=r["outlook"], watch=r["watch"],
                                          withdrawn=r["withdrawn"])))
                report["history_entries"] += 1

    # key financial indicators: Crisil Ratings-adjusted, never compared with reported
    for path, rows in _find(tables, _is_kfi):
        first_cell = rows[min(rows)][min(rows[min(rows)])]
        at = units.index(first_cell)
        heading = next((u for u in reversed(units[:at]) if KFI_HEADING.search(flat(u))), None)
        if heading is None:
            report["unreadable"].append(f"key financial indicators at {path}: no adjusted heading")
            continue
        scope = KFI_HEADING.search(flat(heading)).group(1).lower()
        header = rows[min(rows)]
        periods = {c: period_date(flat(header[c])) for c in header if c >= 3}
        for n, row in sorted(rows.items()):
            if n == min(rows) or not {1, 2} <= set(row):
                continue
            metric, unit = flat(row[1]), UNITS.get(flat(row[2]).lower())
            slug = re.sub(r"[^a-z0-9]+", "_", metric.lower()).strip("_")
            for c, when in periods.items():
                value = number(flat(row[c])) if c in row else None
                if when is None or value is None or unit is None:
                    continue
                claims.append(ExtractedClaim(
                    claim_type="financial_indicator",
                    fact_key=f"financial|{scope}|{slug}|{when.isoformat()}",
                    subject=f"{metric} ({scope}, {AGENCY}-adjusted)",
                    value_numeric=value, value_unit=unit, basis="agency_adjusted",
                    as_of_date=when, extractor=EXTRACTOR, extractor_version=VERSION,
                    anchors=[whole(row[1]), whole(row[2]), whole(header[c]), whole(row[c]),
                             whole(heading)]))
        report["sections"].add("key_financial_indicators")

    # bank lenders (low priority)
    for path, rows in _find(tables, _is_lenders):
        for n, row in sorted(rows.items()):
            if n == min(rows) or not {1, 2, 3} <= set(row):
                continue
            amount = number(flat(row[2]))
            if amount is None:
                continue
            facility = " ".join(MARKERS.sub("", flat(row[1])).split())
            lender = flat(row[3])
            claims.append(ExtractedClaim(
                claim_type="debt_instrument",
                fact_key=f"bank_lender|{AGENCY}|{facility.lower()}|{lender.lower()}|{amount}",
                subject=f"{facility} from {lender}", value_numeric=amount,
                value_unit="INR_CRORE", as_of_date=pub, extractor=EXTRACTOR,
                extractor_version=VERSION,
                anchors=[whole(row[c]) for c in sorted(row) if c <= 4],
                debt=DebtDetail(instrument_name=f"{facility} ({lender})",
                                instrument_type=rating_identity(facility)[0],
                                amount_cr=amount, as_of_date=pub)))

    # prose sections: outlook, liquidity, sensitivities
    for i, u in enumerate(units):
        text = flat(u)
        m = OUTLOOK_LINE.match(text)
        if m and "outlook" not in report["sections"]:
            claims.append(ExtractedClaim(
                claim_type="rationale_point", fact_key=f"outlook_statement|{AGENCY}",
                subject=f"{AGENCY} outlook", value_text=m.group(1).capitalize(),
                as_of_date=pub, extractor=EXTRACTOR, extractor_version=VERSION,
                anchors=[span(u, OUTLOOK_LINE, 1)]))
            report["sections"].add("outlook")
        m = LIQUIDITY_LINE.match(text)
        if m and "liquidity" not in report["sections"]:
            grade = span(u, LIQUIDITY_LINE, 1)
            claims.append(ExtractedClaim(
                claim_type="rationale_point", fact_key=f"liquidity_assessment|{AGENCY}",
                subject=f"{AGENCY} liquidity assessment", value_text=m.group(1).capitalize(),
                as_of_date=pub, extractor=EXTRACTOR, extractor_version=VERSION,
                anchors=[grade]))
            prose = next((p for p in units[i + 1:i + 4] if len(flat(p)) > 60), None)
            if prose:
                claims.append(ExtractedClaim(
                    claim_type="rationale_point", fact_key=f"liquidity_detail|{AGENCY}",
                    subject=f"{AGENCY} liquidity narrative", value_text=flat(prose),
                    as_of_date=pub, extractor=EXTRACTOR, extractor_version=VERSION,
                    anchors=[whole(prose)]))
            report["sections"].add("liquidity")
        if text in DRIVERS:
            # Each driver is a headline paragraph (bold on the page) followed by
            # body paragraphs; the headline is short and ends without a full
            # stop. Keyed like the other agencies' drivers, so successive
            # Crisil rationales diff on them.
            section = DRIVERS[text]
            container = u["node_path"].rsplit("/", 1)[0]
            for b in units[i + 1:]:
                if not b["node_path"].startswith(container):
                    break
                head = flat(b)
                if len(head) < 200 and not head.endswith("."):
                    claims.append(ExtractedClaim(
                        claim_type="rationale_point",
                        fact_key=f"rationale|{AGENCY}|{section}|{head.lower()}",
                        subject=f"{AGENCY} {section[:-1]}", value_text=head,
                        as_of_date=pub, extractor=EXTRACTOR, extractor_version=VERSION,
                        anchors=[whole(b)]))
        if text in ("Upward factors:", "Downward factors:"):
            kind = "positive" if text.startswith("Upward") else "negative"
            container = u["node_path"].rsplit("/", 1)[0]
            bullets = []
            for b in units[i + 1:]:
                if not b["node_path"].startswith(container) or flat(b).endswith("factors:"):
                    break
                if "/li" in b["node_path"]:
                    bullets.append(b)
            if bullets:
                claims.append(ExtractedClaim(
                    claim_type="rationale_point",
                    fact_key=f"rating_sensitivity|{AGENCY}|{kind}",
                    subject=f"{AGENCY} {kind} rating factors",
                    value_text=" ".join(flat(b) for b in bullets), as_of_date=pub,
                    extractor=EXTRACTOR, extractor_version=VERSION,
                    anchors=[whole(b) for b in bullets]))

    # "This RR was updated on May 22, 2026": anchored in the innermost unit that
    # states it, so the date is evidence like any other fact.
    stated = [u for u in units if UPDATED_ON.search(flat(u))]
    if stated:
        u = max(stated, key=lambda u: u["node_path"].count("/"))
        when = datetime.strptime(UPDATED_ON.search(flat(u)).group(1), "%B %d, %Y").date()
        claims.append(ExtractedClaim(
            claim_type="rationale_point", fact_key=f"source_updated_on|{AGENCY}",
            subject=f"{AGENCY} states this rationale was updated", value_text=str(when),
            as_of_date=when, extractor=EXTRACTOR, extractor_version=VERSION,
            anchors=[span(u, UPDATED_ON, 1)]))

    return {"claims": claims, "report": report}


def extract(doc_meta, pages: list[dict]) -> list[ExtractedClaim]:
    """`pages` are HTML units ({kind: "html", node_path, text}) from htmldoc.units."""
    return read(doc_meta, pages)["claims"]


def check_coverage(doc_meta, pages: list[dict], claims) -> Coverage:
    """Required: the rating-action table (every row), the instrument annexure
    (every row), the rating-history annexure, the outlook and the liquidity
    grade. Optional yields (financial indicators, sensitivities, lenders,
    withdrawals, key rating drivers) are not required, but any row that was read and could not be
    understood is reported rather than dropped."""
    report = read(doc_meta, pages)["report"]
    cov = rating_rationale_coverage(AGENCY, claims, (
        (f"outlook_statement|{AGENCY}", "outlook"),
        (f"rating_history|{AGENCY}", "rating history annexure entries"),
    ))
    cov.require(report["action_table"], f"{AGENCY}: no rating-action table found")
    for row in sorted(set(report["action_rows"]) - set(report["action_rated"])):
        cov.require(False, f"{AGENCY}: rating-action row {row} yielded no rating")
    cov.require(bool(report["annex_rows"]),
                f"{AGENCY}: no instrument annexure found")
    for row in sorted(set(report["annex_rows"]) - set(report["annex_read"])):
        cov.require(False, f"{AGENCY}: instrument annexure row {row} yielded no instrument")
    for item in report["unreadable"]:
        cov.require(False, f"{AGENCY}: unreadable {item}")
    return cov

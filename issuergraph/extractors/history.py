"""Rating-history annexures: an agency's own record of its past actions.

Every rationale we use closes with a table of the agency's actions on each
instrument over the last three years. Those rows are how we find out which of
the agency's documents we do not hold — the defect that let CARE's March 2024
AA read as current after CARE had revised it twice. Each row becomes an
anchored `rating_history` claim like any other fact, with provenance
'history_annexure', so it can never be mistaken for the rationale's own action.

The tables are laid out three ways, all handled from the page word boxes:

  * dates inside the cells, a date then its rating in reading order, the date
    split over lines — ICRA 2025/2026 ("Sep-/25-/2024", "Sep/24,/2025");
  * dates in the column headers and ratings in the cells — ICRA 2024
    ("Mar 12, 2024"), Brickwork ("30 Sept 2024"); the cells are assigned to a
    date by column position;
  * date inside the rating text — CARE ("1)CARE AA (RWN) (13-Apr-24)"),
    parsed by the CARE extractor over rows built here.

A table is one table even when it runs over a page break: page furniture and
the repeated header are dropped, lines before the first row on a continuation
page belong to the last row of the previous one, and a cell split across the
break (ICRA's "PP-MLD[ICRA]" on one page, "AA (Stable)" on the next) is merged
by column. Evidence is anchored per page fragment, so every anchor still
indexes exactly into one page's text.

The cell under the document's own action date is that document's action, not
history, and is skipped: it is a primary rating action already.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from .common import anchor, normalise_grade, parse_outlook, parse_watch, rating_identity
from ..models import ExtractedClaim, HistoryDetail

MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
SLACK = 3.0                     # points; same column
COLUMN_REACH = 45.0             # points; a cell's centre within this of its header's

# Header dates: "Mar 12, 2024", "Aug 03," + "2022", "September 24," + "2025",
# "30 Sept 2024".
HEADER_MDY = re.compile(r"^(?P<m>[A-Z][a-z]{2,8}) (?P<d>\d{1,2}),(?: (?P<y>\d{4}))?$")
HEADER_DMY = re.compile(r"^(?P<d>\d{1,2}) (?P<m>[A-Z][a-z]{2,8}) (?P<y>\d{4})$")
YEAR = re.compile(r"^\d{4}$")
# Inline dates, one token per line: "Sep-" "25-" "2024" or "Sep" "24," "2025".
INLINE_MONTH = re.compile(r"^(?P<m>[A-Z][a-z]{2})-?$")
INLINE_DAY = re.compile(r"^(?P<d>\d{1,2})[-,]?$")
INLINE_DAY_MONTH = re.compile(r"^(?P<d>\d{1,2})-(?P<m>[A-Za-z]{3})-$")      # "29-DEC-"
HEADER_MONTH = re.compile(r"^(?P<m>[A-Z][a-z]{2,8})$")
HEADER_DAY_YEAR = re.compile(r"^(?P<d>\d{1,2}), (?P<y>\d{4})$")          # "25, 2024"
SERIAL = re.compile(r"^\d{1,2}$")
EMPTY_CELL = re.compile(r"^-$")
REMOVED_WATCH = re.compile(r"removed\s+(?:from\s+)?Rating\s+Watch", re.IGNORECASE)
SPACED_MODIFIER = re.compile(r"(?<=[A-D])\s+([+-])(?=\s|/|$)")
# A narrow column can break the grade itself: "MLD[ICRA]A" / "A (Stable)" is
# AA (ICRA Sep 2024). After an agency marker, two runs of one letter split only
# by the line break are one grade; no rating is written "A A".
SPLIT_GRADE = re.compile(r"(\]|\bCARE\s|\bBWR\s)(?P<a>([A-D])\3{0,2})\s+(?P<b>\3{1,2})(?=[+-]?(?:[\s(;/]|$))")
# ICRA's own tables occasionally drop the closing bracket: "AA (Stable" (Sep 2025).
OPEN_OUTLOOK = re.compile(r"\((Stable|Negative|Positive|Developing)\b(?!\s+Implications)")
# CARE writes the outlook after a semicolon: "CARE AA; Stable (28-Sep-23)".
SEMICOLON_OUTLOOK = re.compile(r";\s*(Stable|Negative|Positive|Developing)\b(?!\s+Implications)")
# A bare 1-3 digit line outside the name column is a page number, never a
# rating or a date (years are four digits).
STRAY_NUMBER = re.compile(r"^\d{1,3}$")


def month_day_year(month: str, day: str, year: str) -> date | None:
    number = MONTHS.get(month[:3].lower())
    if number is None:
        return None
    try:
        return date(int(year), number, int(day))
    except ValueError:
        return None


# --- lines and rows -----------------------------------------------------------

def _page_lines(page: dict, begin: int, end: int, furniture: re.Pattern | None) -> list[dict]:
    text, word_map = page["text"], page.get("word_map")
    if word_map is None:
        raise ValueError("history annexure parsing needs the page word_map")
    words = sorted(word_map, key=lambda w: w[0])
    lines, pos = [], begin
    for raw in text[begin:end].split("\n"):
        start, stop = pos, pos + len(raw)
        pos = stop + 1
        if not raw.strip() or (furniture and furniture.search(raw)):
            continue
        boxes = [w for w in words if start <= w[0] < stop]
        if not boxes:
            continue
        lines.append({"page_no": page["page_no"], "text": raw.strip(), "start": start,
                      "end": stop, "x0": boxes[0][2], "x1": max(w[4] for w in boxes),
                      "y": boxes[0][3]})
    return lines


def _line_at(lines: list[dict], pos: int) -> dict | None:
    return next((l for l in lines if l["start"] <= pos <= l["end"]), None)


def table_lines(pages: list[dict], head: str, ends: tuple[str, ...],
                furniture: re.Pattern | None = None) -> tuple[list[dict], list[dict]]:
    """(header lines, body lines) of a table that may continue over pages.

    Bounded by position on the page, not by text offset: PyMuPDF can emit a
    later section before the table's continuation (ICRA 2024 prints its
    complexity table below the history table's last rows, but first in text
    order). The header is everything before the first row; a continuation page
    repeats it, and that repeated prefix is dropped by text.
    """
    header: list[dict] = []
    body: list[dict] = []
    started = False
    for page in pages:
        text = page["text"]
        top = float("-inf")
        if not started:
            at = text.find(head)
            if at == -1:
                continue                # pages before the table are never read
            started, first = True, True
            lines = _page_lines(page, 0, len(text), furniture)
            top = _line_at(lines, at)["y"]
        else:
            first = False
            lines = _page_lines(page, 0, len(text), furniture)
        stops = []
        for marker in ends:
            pos = text.find(marker)
            while pos != -1:
                line = _line_at(lines, pos + (1 if marker.startswith("\n") else 0))
                if line and line["y"] > top:
                    stops.append(line["y"])
                    break
                pos = text.find(marker, pos + 1)
        bottom = min(stops) if stops else float("inf")
        lines = [l for l in lines if top < l["y"] < bottom]
        if first:
            cut = _first_row(lines)
            header, lines = lines[:cut], lines[cut:]
        else:
            # the repeated header, and a page number printed above it
            seen = {h["text"] for h in header}
            while lines and (lines[0]["text"] in seen or STRAY_NUMBER.match(lines[0]["text"])):
                lines.pop(0)
        body.extend(lines)
        if stops:
            break
    return header, body


LABEL_HEADER = re.compile(r"^(Instrument|Name of the)\b")
TYPE_HEADER = re.compile(r"^Type$")


def _centre(line: dict) -> float:
    return (line["x0"] + line["x1"]) / 2


def label_band(header: list[dict], lines: list[dict]) -> float:
    """Right edge of the instrument-name column.

    Names are left-aligned in some tables and centred in others (ICRA 2026
    centres "NCD" over "programme"), so "the leftmost line" misses rows. Every
    layout has a Type column next to the names: a line is a name line when its
    centre lies left of the midpoint between the name header and Type header.
    """
    kind = next((h for h in header if TYPE_HEADER.match(h["text"])), None)
    name = next((h for h in header if LABEL_HEADER.match(h["text"])), None)
    if kind is None:
        return min(l["x0"] for l in lines) + SLACK if lines else 0.0
    if name is None:
        return kind["x0"]
    return (_centre(name) + _centre(kind)) / 2


def _is_header_text(line: dict) -> bool:
    return bool(LABEL_HEADER.match(line["text"]) or line["text"] in ("Sr.", "No.", "Sr. No."))


def _first_row(lines: list[dict]) -> int:
    """Index of the first line that starts a data row: the first name-column
    line after the Type header. Everything before it is header."""
    kind = next((i for i, l in enumerate(lines) if TYPE_HEADER.match(l["text"])), None)
    if kind is None:
        return 0
    band = label_band(lines[:kind + 1], lines)
    for i in range(kind + 1, len(lines)):
        if _centre(lines[i]) < band and not _is_header_text(lines[i]):
            return i
    return len(lines)


TYPE_CELL = re.compile(r"^(Long|Short|Long[- ]term|Short[- ]term|term|Term|LT|ST)$")


def rows(header: list[dict], body: list[dict]) -> list[dict]:
    """Group body lines into rows.

    Where the table numbers its rows, a row starts at its serial number.
    Otherwise it starts at its Type cell ("Long / term"), which every row has:
    the instrument name is not reliable for this, because a row that straddles
    a page break can print its name on the next page (ICRA 2024: the NCD row's
    type, amount and first entries at the foot of page 6, its name at the top
    of page 7). A name block not followed by its own Type cell is that orphan,
    and belongs to the row still missing one. Lines before the first row on a
    continuation page belong to the last row, which is why the body is one
    sequence across pages.
    """
    band = label_band(header, body)
    in_band = [_centre(l) < band for l in body]
    serial = [in_band[i] and SERIAL.match(l["text"]) is not None for i, l in enumerate(body)]
    if any(serial):
        starts = [i for i, is_serial in enumerate(serial) if is_serial]
        out = []
        for n, s in enumerate(starts):
            chunk = body[s + 1:starts[n + 1] if n + 1 < len(starts) else len(body)]
            label = [l for l in chunk if _centre(l) < band]
            cells = [l for l in chunk if _centre(l) >= band and not STRAY_NUMBER.match(l["text"])]
            out.append({"label": label, "cells": cells})
        return _named(out)

    kind = next((h for h in header if TYPE_HEADER.match(h["text"])), None)
    kind_x = _centre(kind) if kind else None

    def is_type(i: int) -> bool:
        line = body[i]
        return (kind_x is not None and not in_band[i] and TYPE_CELL.match(line["text"])
                and abs(_centre(line) - kind_x) <= COLUMN_REACH / 2)

    out: list[dict] = []
    pending: list[dict] = []            # name lines not yet claimed by a row
    i = 0
    while i < len(body):
        line = body[i]
        if in_band[i]:
            pending.append(line)
        elif is_type(i):
            row = {"label": pending, "cells": []}
            pending = []
            while i < len(body) and is_type(i):
                row["cells"].append(body[i])
                i += 1
            out.append(row)
            continue
        else:
            if pending:                 # an orphan name: the row missing one
                owner = next((r for r in reversed(out) if not r["label"]), None)
                (owner or (out[-1] if out else {"label": []}))["label"].extend(pending)
                pending = []
            if out and not STRAY_NUMBER.match(line["text"]):
                out[-1]["cells"].append(line)
        i += 1
    if pending:
        owner = next((r for r in reversed(out) if not r["label"]), None)
        if owner:
            owner["label"].extend(pending)
    return _named(out)


def _named(out: list[dict]) -> list[dict]:
    for row in out:
        row["name"] = " ".join(l["text"] for l in row["label"])
    return out


def runs(lines: list[dict]) -> list[list[dict]]:
    """Split lines into anchorable runs: same page and adjacent in the text."""
    out: list[list[dict]] = []
    for line in lines:
        prev = out[-1][-1] if out else None
        if prev and prev["page_no"] == line["page_no"] and line["start"] == prev["end"] + 1:
            out[-1].append(line)
        else:
            out.append([line])
    return out


def anchors_for(lines: list[dict], texts: dict[int, str]):
    return [anchor(r[0]["page_no"], texts[r[0]["page_no"]], r[0]["start"], r[-1]["end"])
            for r in runs(lines)]


# --- dates ----------------------------------------------------------------------

def header_dates(header: list[dict]) -> list[dict]:
    """Column-header dates with the lines that state them."""
    found, i = [], 0
    while i < len(header):
        line = header[i]
        m = HEADER_DMY.match(line["text"])
        if m:
            d = month_day_year(m["m"], m["d"], m["y"])
            if d:
                found.append({"date": d, "lines": [line]})
            i += 1
            continue
        m = HEADER_MONTH.match(line["text"])
        n = HEADER_DAY_YEAR.match(header[i + 1]["text"]) if m and i + 1 < len(header) else None
        if m and n:
            d = month_day_year(m["m"], n["d"], n["y"])
            if d:
                found.append({"date": d, "lines": [line, header[i + 1]]})
            i += 2
            continue
        m = HEADER_MDY.match(line["text"])
        if m:
            year, lines = m["y"], [line]
            if not year and i + 1 < len(header) and YEAR.match(header[i + 1]["text"]):
                year, lines = header[i + 1]["text"], [line, header[i + 1]]
            d = month_day_year(m["m"], m["d"], year) if year else None
            if d:
                found.append({"date": d, "lines": lines})
            i += len(lines)
            continue
        i += 1
    for h in found:
        h["x0"] = min(l["x0"] for l in h["lines"])
        h["x1"] = max(l["x1"] for l in h["lines"])
    return found


def _inline_date(cells: list[dict], i: int):
    """An inline date starting at cells[i]: (date, lines) or None."""
    if i + 1 < len(cells):
        dm = INLINE_DAY_MONTH.match(cells[i]["text"])
        if dm and YEAR.match(cells[i + 1]["text"]):
            when = month_day_year(dm["m"], dm["d"], cells[i + 1]["text"])
            return (when, cells[i:i + 2]) if when else None
    if i + 2 >= len(cells):
        return None
    m, d, y = cells[i]["text"], cells[i + 1]["text"], cells[i + 2]["text"]
    mm, dd = INLINE_MONTH.match(m), INLINE_DAY.match(d)
    if not (mm and dd and YEAR.match(y)):
        return None
    when = month_day_year(mm["m"], dd["d"], y)
    return (when, cells[i:i + 3]) if when else None


# --- entries --------------------------------------------------------------------

def read_rating(text: str) -> dict:
    """Grade, outlook, watch and withdrawal from one history cell's text."""
    compact = SPACED_MODIFIER.sub(r"\1", text)          # Brickwork writes "AA +"
    compact = SPLIT_GRADE.sub(lambda m: m.group(1) + m.group("a") + m.group("b"), compact)
    watch = None if REMOVED_WATCH.search(text) else parse_watch(compact)
    outlook = parse_outlook(compact)
    if outlook is None and watch is None:
        m = OPEN_OUTLOOK.search(compact) or SEMICOLON_OUTLOOK.search(compact)
        outlook = m.group(1).capitalize() if m else None
    return {"grade": normalise_grade(compact), "outlook": outlook,
            "watch": watch, "withdrawn": "withdrawn" in text.casefold()}


def table_entries(header: list[dict], body: list[dict]) -> tuple[list[dict], list[str]]:
    """Entries from dates in cells or in headers. Returns (entries, unreadable).

    `unreadable` names every dated cell that stated neither a grade nor a
    withdrawal: a history row we failed to read is reported, never dropped.
    """
    columns = header_dates(header)
    entries, unreadable = [], []
    for row in rows(header, body):
        cells, used = row["cells"], set()

        # dates inside the cells: the rating follows its date
        i = 0
        while i < len(cells):
            hit = _inline_date(cells, i)
            if not hit:
                i += 1
                continue
            when, date_lines = hit
            j = i + len(date_lines)
            rating = []
            while j < len(cells) and not _inline_date(cells, j) \
                    and not EMPTY_CELL.match(cells[j]["text"]):
                rating.append(cells[j])
                j += 1
            used.update(id(l) for l in date_lines + rating)
            entries.append({"row": row, "date": when, "date_lines": date_lines,
                            "rating_lines": rating})
            i = j

        # dates in the headers: a cell belongs to the column whose centre it shares
        by_column: dict[int, list[dict]] = {}
        for cell in cells:
            if id(cell) in used or EMPTY_CELL.match(cell["text"]) or not columns:
                continue
            centre = (cell["x0"] + cell["x1"]) / 2
            dist, n = min((abs(centre - (h["x0"] + h["x1"]) / 2), n)
                          for n, h in enumerate(columns))
            if dist <= COLUMN_REACH:
                by_column.setdefault(n, []).append(cell)
        for n, lines in sorted(by_column.items()):
            entries.append({"row": row, "date": columns[n]["date"],
                            "date_lines": columns[n]["lines"], "rating_lines": lines})

    kept = []
    for e in entries:
        e["rating_text"] = " ".join(l["text"] for l in e["rating_lines"])
        e.update(read_rating(e["rating_text"]))
        if e["grade"] or e["withdrawn"]:
            kept.append(e)
        elif e["rating_lines"]:
            unreadable.append(f"{e['row']['name']!r} {e['date']}: {e['rating_text']!r}")
    return kept, unreadable


def history_claims(agency: str, extractor: str, version: str, pages: list[dict],
                   entries: list[dict], own_date: date | None) -> list[ExtractedClaim]:
    """Anchored rating_history claims; the document's own action is not history."""
    texts = {p["page_no"]: p["text"] for p in pages}
    claims = []
    for e in entries:
        if own_date and e["date"] == own_date:
            continue
        label = e["row"]["name"]
        instrument_class, term = rating_identity(label, e["grade"])
        anchors = (anchors_for(e["row"]["label"], texts)
                   + anchors_for(e["date_lines"], texts)
                   + anchors_for(e["rating_lines"], texts))
        claims.append(ExtractedClaim(
            claim_type="rating_history",
            fact_key=(f"rating_history|{agency}|{instrument_class}|{term}|"
                      f"{e['date'].isoformat()}|{label.lower()}"),
            subject=f"{label} — {agency} rating history, {e['date'].isoformat()}",
            value_text=e["rating_text"] or "Withdrawn", as_of_date=e["date"],
            extractor=extractor, extractor_version=version, anchors=anchors,
            history=HistoryDetail(agency=agency, instrument=label,
                                  instrument_class=instrument_class, term=term,
                                  action_date=e["date"], grade=e["grade"],
                                  outlook=e["outlook"], watch=e["watch"],
                                  withdrawn=e["withdrawn"]),
        ))
    return claims


def parse_dmy_short(token: str) -> date | None:
    """CARE's "13-Apr-24", possibly broken over a line ("13-Apr-\\n24")."""
    try:
        return datetime.strptime("".join(token.split()), "%d-%b-%y").date()
    except ValueError:
        return None


def read_annexure(agency: str, extractor: str, version: str, pages: list[dict],
                  own_date: date | None, head: str, ends: tuple[str, ...],
                  furniture: re.Pattern | None = None):
    """(claims, unreadable) for a table whose dates are in cells or headers."""
    header, body = table_lines(pages, head, ends, furniture)
    entries, unreadable = table_entries(header, body)
    return history_claims(agency, extractor, version, pages, entries, own_date), unreadable


def history_coverage(agency: str, claims, unreadable: list[str]):
    """The annexure must yield entries, and every dated cell must be read.

    An annexure that stopped parsing would otherwise make every missing
    document invisible again — the silence this whole pass exists to end.
    """
    from .coverage import Coverage

    cov = Coverage()
    cov.require(any(c.claim_type == "rating_history" for c in claims),
                f"{agency}: no rating-history entries — the history annexure did not parse")
    for cell in unreadable:
        cov.require(False, f"{agency}: unreadable rating-history cell {cell}")
    return cov

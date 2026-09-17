"""Did the extractor find what this document was supposed to contain?

The failure this addresses is silence. An extractor is a set of regexes over a
publisher's current layout; when the layout moves, the regex stops matching and
the extractor returns fewer claims — or none — and every remaining claim is
still perfectly anchored. Nothing raises. The document loads clean.

Downstream that is worse than an error, because a fact that vanished from
extraction is indistinguishable from a fact the issuer stopped reporting. The
surveillance product would show "no longer disclosed" when the truth is
"no longer parsed".

The fix is to state, per extractor, what the document must yield, and to check
it. A document that does not meet its own declaration is stored with
`extraction_status = 'incomplete'` and the specific reasons — not dropped, not
silently accepted. Claims already extracted stay: they are still evidence. What
changes is that the gap is now a fact in the database rather than an absence.

Extractors declare requirements by implementing `check_coverage(doc_meta, pages,
claims) -> Coverage`. An extractor that does not implement one falls back to the
weakest possible check (it produced something at all), which is honest about how
little that proves.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Coverage:
    """What was expected, what was found, and what is missing.

    `missing` is a list of human-readable reasons rather than codes, because its
    audience is whoever is asked why a number disappeared from a report.
    """

    expected: int = 0
    found: int = 0
    missing: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def status(self) -> str:
        return "complete" if self.complete else "incomplete"

    def require(self, condition: bool, reason: str) -> bool:
        """Record one requirement. Returns the condition so callers can branch."""
        self.expected += 1
        if condition:
            self.found += 1
        else:
            self.missing.append(reason)
        return condition

    def merge(self, other: "Coverage") -> "Coverage":
        self.expected += other.expected
        self.found += other.found
        self.missing.extend(other.missing)
        return self

    def summary(self) -> str:
        head = f"{self.found}/{self.expected} expected facts"
        return head if self.complete else f"{head}; missing: " + "; ".join(self.missing)


def default_coverage(claims) -> Coverage:
    """The fallback for an extractor that declares no requirements.

    Producing zero claims from a document we deliberately chose to ingest is
    always wrong. Producing some claims proves only that; that weakness is the
    argument for extractors declaring real requirements.
    """
    cov = Coverage()
    cov.require(bool(claims), "extractor produced no claims at all")
    return cov


def rating_rationale_coverage(agency: str, claims,
                              sections: tuple[tuple[str, str], ...],
                              liquidity: bool = True) -> Coverage:
    """What every rating rationale must yield, section by section.

    "Produced something" let a rationale whose rating table had stopped parsing
    pass as complete on the strength of its bullet points — 87 claims, zero
    ratings, status complete. Each essential section is therefore its own
    requirement, so the report of a shortfall names the section that failed.

    `sections` are (fact_key prefix, human name) pairs beyond the rating table
    and liquidity assessment that this agency's layout always carries.
    """
    cov = Coverage()
    keys = [c.fact_key for c in claims]
    cov.require(any(c.claim_type == "rating" for c in claims),
                f"{agency}: no rating found — the summary/rating table did not parse")
    if liquidity:
        cov.require(any(k.startswith(f"liquidity_assessment|{agency}") for k in keys),
                    f"{agency}: no liquidity assessment found")
    for prefix, name in sections:
        cov.require(any(k.startswith(prefix) for k in keys),
                    f"{agency}: no {name} found")
    return cov


def check(extractor_module, doc_meta, pages, claims) -> Coverage:
    checker = getattr(extractor_module, "check_coverage", None)
    if checker is None:
        return default_coverage(claims)
    return checker(doc_meta, pages, claims)

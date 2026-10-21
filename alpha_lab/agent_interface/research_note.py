"""ResearchNote: what an agent writes after reading AlphaLab, and the check
that it only cites what it was actually shown (roadmap Phase 9, PR C).

A note makes claims and cites evidence by id. The validator reads the call log
(`call_log.py`: the exact responses the agent received) and rejects a note when
a citation was never served *for that ticker and as-of date*, when a quoted
figure differs from the served value, or when its stated data freshness is not
what it was told. It checks provenance, not truth: a note can still be wrong
about what served evidence means; it just cannot cite evidence it invented, took
from another security or date, or never requested.

Reads only (the log is opened read-only by the CLI); never raises on a bad note.
"""

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from alpha_lab.agent_interface.call_log import CallLog
from alpha_lab.agent_interface.safety import (
    clean_text,
    collect_provenance_ids,
    has_unsafe_characters,
)

MAX_CLAIMS = 30
MAX_CITATIONS_PER_CLAIM = 10
MAX_ID_CHARS = 160

# Tools whose answer is about one ticker (the log's `ticker` column).
SINGLE_TICKER_TOOLS = frozenset({
    "get_research_summary", "get_domain", "get_conviction", "get_conviction_history",
    "list_evidence", "get_evidence",
})
# Keys under which a served response lists evidence ids.
_EVIDENCE_LIST_KEYS = frozenset({"evidence_ids", "supporting_evidence", "supporting_evidence_ids"})


class CitationKind(StrEnum):
    PROVENANCE = "provenance"
    EVIDENCE = "evidence"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Citation(_Strict):
    kind: CitationKind
    id: str = Field(min_length=1, max_length=MAX_ID_CHARS)


class Quote(_Strict):
    """A figure the claim states, tied to the evidence item it came from."""

    evidence_id: str = Field(min_length=1, max_length=MAX_ID_CHARS)
    value: str | int | float | bool | None


class Claim(_Strict):
    text: str = Field(min_length=1, max_length=600)
    citations: list[Citation] = Field(min_length=1, max_length=MAX_CITATIONS_PER_CLAIM)
    quotes: list[Quote] = Field(default_factory=list, max_length=10)


class NoteFreshness(_Strict):
    """The data freshness the agent states; must equal what it was served."""

    price_data_through: date | None
    price_status: Literal["fresh", "stale", "missing"]


class ResearchNote(_Strict):
    ticker: str = Field(min_length=1, max_length=14, pattern=r"^[A-Za-z0-9.\-^=]+$")
    as_of: date | None = None  # None: a live (current-date) note
    summary: str = Field(min_length=1, max_length=1_200)
    claims: list[Claim] = Field(min_length=1, max_length=MAX_CLAIMS)
    freshness: NoteFreshness
    limitations: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("ticker", mode="before")
    @classmethod
    def upper(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("limitations")
    @classmethod
    def short_limitations(cls, values: list[str]) -> list[str]:
        if any(len(item) > 300 for item in values):
            raise ValueError("each limitation must be at most 300 characters")
        return values


class NoteIssue(BaseModel):
    code: str
    message: str
    claim_index: int | None = None
    citation_id: str | None = None


class NoteValidation(BaseModel):
    ok: bool
    issues: list[NoteIssue] = Field(default_factory=list)
    calls_considered: int = 0
    calls_in_scope: int = 0
    citations_checked: int = 0


# --- what was served ----------------------------------------------------------------

@dataclass
class _Scope:
    """What one logged call served for one (ticker, as_of)."""

    call_id: int
    ticker: str
    requested_as_of: str | None
    provenance: set[str] = field(default_factory=set)
    evidence: set[str] = field(default_factory=set)
    evidence_values: dict[str, Any] = field(default_factory=dict)
    freshness: dict[str, Any] | None = None


def _plain(value: Any) -> Any:
    """A served figure as the agent saw it (wrapped text unwrapped)."""
    if isinstance(value, dict) and value.get("untrusted") is True and "text" in value:
        return value["text"]
    return value


def _collect_evidence(node: Any, evidence: set[str], values: dict[str, Any], depth: int = 0) -> None:
    if depth > 14:
        return
    if isinstance(node, dict):
        evidence_id = node.get("evidence_id")
        if isinstance(evidence_id, str) and evidence_id:
            evidence.add(evidence_id)
            if "value" in node:
                values[evidence_id] = _plain(node["value"])
        for key, value in node.items():
            if key in _EVIDENCE_LIST_KEYS and isinstance(value, list):
                evidence.update(item for item in value if isinstance(item, str) and item)
            else:
                _collect_evidence(value, evidence, values, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _collect_evidence(item, evidence, values, depth + 1)


def _scopes_of(entry: dict[str, Any]) -> list[_Scope]:
    response, tool = entry["response"], entry["tool"]
    as_of = entry["requested_as_of"]
    data = response.get("data")
    scopes: list[_Scope] = []
    if tool in SINGLE_TICKER_TOOLS and entry["ticker"]:
        scope = _Scope(entry["id"], entry["ticker"].upper(), as_of)
        scope.provenance.update(response.get("provenance_ids") or [])
        scope.provenance.update(collect_provenance_ids(data))
        _collect_evidence(data, scope.evidence, scope.evidence_values)
        freshness = response.get("freshness")
        if isinstance(freshness, dict) and "price_status" in freshness:
            scope.freshness = {"price_data_through": freshness.get("price_data_through"),
                               "price_status": freshness["price_status"]}
        scopes.append(scope)
    elif tool == "compare" and isinstance(data, dict):
        for row in data.get("rows") or []:
            if not isinstance(row, dict) or not isinstance(row.get("ticker"), str):
                continue
            scope = _Scope(entry["id"], row["ticker"].upper(), as_of)
            scope.provenance.update(collect_provenance_ids(row))
            if "price_status" in row:
                scope.freshness = {"price_data_through": row.get("price_data_through"),
                                   "price_status": row["price_status"]}
            scopes.append(scope)
    return scopes


def _same_value(served: Any, stated: Any) -> bool:
    if isinstance(served, bool) or isinstance(stated, bool):
        return served is stated
    if isinstance(served, (int, float)) and isinstance(stated, (int, float)):
        return abs(served - stated) <= 1e-9 * max(1.0, abs(served))
    if isinstance(served, str) and isinstance(stated, str):
        return clean_text(served) == clean_text(stated)
    return served == stated


# --- validation -----------------------------------------------------------------------

def validate_note(
    raw: dict[str, Any] | str,
    log: CallLog,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    client: str | None = None,
) -> NoteValidation:
    """Check a note against the calls in `[since, until]` (optionally one
    `client`). Returns a report; never raises for a bad note."""
    issues: list[NoteIssue] = []
    try:
        document = json.loads(raw) if isinstance(raw, str) else raw
        note = ResearchNote.model_validate(document)
    except (json.JSONDecodeError, ValidationError, TypeError) as error:
        detail = error.errors()[0] if isinstance(error, ValidationError) else None
        where = ".".join(str(part) for part in detail["loc"]) if detail else ""
        message = f"{where}: {detail['msg']}" if detail else f"not a valid note ({type(error).__name__})"
        return NoteValidation(ok=False, issues=[NoteIssue(code="schema", message=message[:300])])

    for index, claim in enumerate(note.claims):
        if has_unsafe_characters(claim.text):
            issues.append(NoteIssue(code="unsafe_text", message="claim text holds control or bidirectional characters",
                                    claim_index=index))
    for label, text in [("summary", note.summary), *[("limitation", t) for t in note.limitations]]:
        if has_unsafe_characters(text):
            issues.append(NoteIssue(code="unsafe_text", message=f"{label} holds control or bidirectional characters"))

    entries = [e for e in log.entries_between(since=since, until=until, client=client) if e["ok"]]
    as_of = None if note.as_of is None else note.as_of.isoformat()
    scopes = [scope for entry in entries for scope in _scopes_of(entry)]
    in_scope = [s for s in scopes if s.ticker == note.ticker and s.requested_as_of == as_of]
    others = [s for s in scopes if not (s.ticker == note.ticker and s.requested_as_of == as_of)]
    served_provenance = {i for s in in_scope for i in s.provenance}
    served_evidence = {i for s in in_scope for i in s.evidence}
    values: dict[str, Any] = {}
    for scope in sorted(in_scope, key=lambda s: s.call_id):  # the latest served value wins
        values.update(scope.evidence_values)

    def elsewhere(citation: Citation) -> str | None:
        pool = (lambda s: s.provenance) if citation.kind is CitationKind.PROVENANCE else (lambda s: s.evidence)
        hit = next((s for s in others if citation.id in pool(s)), None)
        return None if hit is None else f"{hit.ticker}, as_of {hit.requested_as_of or 'live'}"

    checked = 0
    for index, claim in enumerate(note.claims):
        for citation in claim.citations:
            checked += 1
            served = served_provenance if citation.kind is CitationKind.PROVENANCE else served_evidence
            if citation.id in served:
                continue
            other_kind = served_evidence if citation.kind is CitationKind.PROVENANCE else served_provenance
            if citation.id in other_kind:
                issues.append(NoteIssue(
                    code="wrong_kind", claim_index=index, citation_id=citation.id,
                    message=f"{citation.id!r} was served as "
                            f"{'evidence' if citation.kind is CitationKind.PROVENANCE else 'provenance'}, "
                            f"not as {citation.kind.value}"))
            elif (where := elsewhere(citation)) is not None:
                issues.append(NoteIssue(
                    code="served_for_other_scope", claim_index=index, citation_id=citation.id,
                    message=f"{citation.id!r} was served for {where}, not for {note.ticker} "
                            f"as_of {as_of or 'live'}"))
            else:
                issues.append(NoteIssue(
                    code="never_served", claim_index=index, citation_id=citation.id,
                    message=f"{citation.kind.value} id {citation.id!r} was never served in the considered calls"))
        for quote in claim.quotes:
            if quote.evidence_id not in served_evidence:
                issues.append(NoteIssue(
                    code="never_served", claim_index=index, citation_id=quote.evidence_id,
                    message=f"quoted evidence {quote.evidence_id!r} was never served for {note.ticker}"))
            elif quote.evidence_id not in values:
                issues.append(NoteIssue(
                    code="value_not_served", claim_index=index, citation_id=quote.evidence_id,
                    message=f"{quote.evidence_id!r} was served by id only; its value was never shown"))
            elif not _same_value(values[quote.evidence_id], quote.value):
                issues.append(NoteIssue(
                    code="value_mismatch", claim_index=index, citation_id=quote.evidence_id,
                    message=f"{quote.evidence_id!r} was served as {values[quote.evidence_id]!r}, "
                            f"the note states {quote.value!r}"))

    stated = note.freshness
    latest = next((s.freshness for s in sorted(in_scope, key=lambda s: s.call_id, reverse=True) if s.freshness), None)
    if latest is None:
        issues.append(NoteIssue(code="freshness_not_served",
                                message=f"no freshness was served for {note.ticker} as_of {as_of or 'live'}"))
    else:
        served_through = latest.get("price_data_through")
        served_date = None if served_through in (None, "") else date.fromisoformat(str(served_through)[:10])
        if served_date != stated.price_data_through or latest.get("price_status") != stated.price_status:
            issues.append(NoteIssue(
                code="freshness_mismatch",
                message=f"served: price data through {served_date}, {latest.get('price_status')}; "
                        f"the note states {stated.price_data_through}, {stated.price_status}"))

    return NoteValidation(ok=not issues, issues=issues, calls_considered=len(entries),
                          calls_in_scope=len({s.call_id for s in in_scope}), citations_checked=checked)

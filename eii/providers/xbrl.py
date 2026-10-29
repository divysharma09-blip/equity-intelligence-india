"""Parser for SEBI/NSE financial-results XBRL instance documents.

Works for both the legacy (`in-bse-fin`, 2019–2024) and the Integrated Filing
(`in-capmkt`, 2025+) taxonomies by matching element *local names* and choosing
contexts by their period, not by their (filer-specific) ids:

* annual  = a dimension-free duration context of ~12 months ending at the reporting period end
* instant = a dimension-free instant context at the reporting period end (balance sheet)

Nothing is rescaled or inferred here: values are taken as filed (XBRL stores
monetary amounts in base units, i.e. rupees). Ratios outside [-1, 1] are
rejected rather than "corrected".
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

XBRLI = "{http://www.xbrl.org/2003/instance}"


class XbrlParseError(ValueError):
    pass


@dataclass
class XbrlFiling:
    format: str                       # industrial | bank | nbfc | insurer | unknown
    period_end: date
    period_start: Optional[date]
    basis: Optional[str]              # Consolidated / Standalone
    currency: Optional[str]
    annual: dict[str, float] = field(default_factory=dict)
    instant: dict[str, float] = field(default_factory=dict)
    text: dict[str, str] = field(default_factory=dict)
    lists: dict[str, list[str]] = field(default_factory=dict)  # repeated text items (e.g. auditors)
    rejected: list[str] = field(default_factory=list)  # data-quality notes about this filing
    has_annual_context: bool = False


def detect_format(url_or_name: str, schema_ref: str = "") -> str:
    probe = f"{url_or_name} {schema_ref}".upper()
    if "BANKING" in probe:
        return "bank"
    if "NBFC" in probe:
        return "nbfc"
    if any(t in probe for t in ("_LI_", "_GI_", "INSURANCE", "LIFEINS", "GENERALINS")):
        return "insurer"
    if "INDAS" in probe or "IND-AS" in probe:
        return "industrial"
    return "unknown"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _d(text: Optional[str]) -> Optional[date]:
    try:
        return date.fromisoformat(text.strip()) if text else None
    except ValueError:
        return None


RATIO_ELEMENTS = {"PercentageOfGrossNpa", "PercentageOfNpa", "ReturnOnAssets", "CET1Ratio",
                  "AdditionalTier1Ratio"}


def parse_results_xbrl(content: bytes, source_name: str = "") -> XbrlFiling:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise XbrlParseError(f"Not a valid XBRL document: {exc}") from exc
    schema = next((el.get("{http://www.w3.org/1999/xlink}href", "") for el in root
                   if _local(el.tag) == "schemaRef"), "")
    ns_text = content[:4000].decode("utf-8", errors="ignore")  # namespace declarations name the taxonomy

    # ---- contexts
    contexts: dict[str, dict] = {}
    for ctx in root.iter(f"{XBRLI}context"):
        has_dims = any(_local(e.tag) in ("segment", "scenario") for e in ctx.iter())
        period = ctx.find(f"{XBRLI}period")
        if period is None:
            continue
        contexts[ctx.get("id")] = {
            "dims": has_dims,
            "start": _d(getattr(period.find(f"{XBRLI}startDate"), "text", None)),
            "end": _d(getattr(period.find(f"{XBRLI}endDate"), "text", None)),
            "instant": _d(getattr(period.find(f"{XBRLI}instant"), "text", None)),
        }

    # ---- raw facts
    raw: list[tuple[str, str, str]] = []
    for el in root:
        ref = el.get("contextRef")
        if ref is None or el.text is None:
            continue
        raw.append((_local(el.tag), ref, el.text.strip()))

    def first_text(name: str) -> Optional[str]:
        return next((v for n, _, v in raw if n == name and v), None)

    reporting_ends = [d for d in (_d(v) for n, _, v in raw if n == "DateOfEndOfReportingPeriod") if d]
    context_ends = [c["end"] for c in contexts.values() if c["end"] and not c["dims"]]
    period_end = (max(reporting_ends) if reporting_ends
                  else _d(first_text("DateOfEndOfFinancialYear")) or max(context_ends, default=None))
    if period_end is None:
        raise XbrlParseError("Could not determine the reporting period end date.")

    # Period each context *declares* through its own DateOfStart/EndOfReportingPeriod facts.
    declared: dict[str, dict[str, Optional[date]]] = {}
    for n, ref, v in raw:
        if n == "DateOfStartOfReportingPeriod":
            declared.setdefault(ref, {})["start"] = _d(v)
        elif n == "DateOfEndOfReportingPeriod":
            declared.setdefault(ref, {})["end"] = _d(v)
    fy_start = _d(first_text("DateOfStartOfFinancialYear"))
    fy_end = _d(first_text("DateOfEndOfFinancialYear"))

    def is_year(start: Optional[date], end: Optional[date]) -> bool:
        return bool(start and end and end == period_end and 330 <= (end - start).days <= 380)

    referenced = {ref for _, ref, _ in raw}
    annual_ids: set[str] = set()
    for ref in referenced:
        c, dcl = contexts.get(ref), declared.get(ref, {})
        if c is not None and c["dims"]:
            continue
        if is_year(dcl.get("start"), dcl.get("end")):
            annual_ids.add(ref)                      # declared 12-month period (legacy files)
        elif c is not None and is_year(c["start"], c["end"]):
            annual_ids.add(ref)                      # well-formed context period
    legacy_convention = False
    if not annual_ids and "FourD" in referenced and fy_end == period_end:
        # Legacy convention: "FourD" = year-to-date. In a filing whose financial year ends at the
        # reporting period end, that is the full year. Used only when contexts are missing/mis-dated.
        annual_ids.add("FourD")
        legacy_convention = True
    instant_ids = {cid for cid, c in contexts.items() if not c["dims"] and c["instant"] == period_end}
    if not instant_ids and "OneI" in referenced and ("OneI" not in contexts or contexts["OneI"]["instant"] is None):
        instant_ids.add("OneI")
        legacy_convention = True
    start = next((declared.get(c, {}).get("start") or (contexts.get(c) or {}).get("start")
                  for c in annual_ids), None) or (fy_start if annual_ids else None)

    filing = XbrlFiling(
        format=detect_format(source_name, schema + " " + ns_text),
        period_end=period_end, period_start=start,
        basis=None, currency=first_text("DescriptionOfPresentationCurrency"),
        has_annual_context=bool(annual_ids))
    if legacy_convention:
        filing.rejected.append("Context periods missing or mis-dated in filing; used NSE legacy context "
                               "conventions (FourD = full year, OneI = year end).")

    for name, ref, value in raw:
        if ref in annual_ids or ref in instant_ids:
            try:
                num = float(value)
            except ValueError:
                if ref in annual_ids:
                    filing.text.setdefault(name, value)
                continue
            if name in RATIO_ELEMENTS and not -1 <= num <= 1:
                filing.rejected.append(f"{name}={value} (ratio outside [-1, 1]; not used)")
                continue
            target = filing.annual if ref in annual_ids else filing.instant
            target.setdefault(name, num)
        elif name == "AuditorsFirmName":
            filing.lists.setdefault(name, []).append(value)

    basis = filing.text.get("NatureOfReportStandaloneConsolidated") or first_text(
        "NatureOfReportStandaloneConsolidated")
    filing.basis = basis.strip().title() if basis else None
    if "DeclarationOfUnmodifiedOpinionOrStatementOnImpactOfAuditQualification" not in filing.text:
        op = first_text("DeclarationOfUnmodifiedOpinionOrStatementOnImpactOfAuditQualification")
        if op:
            filing.text["DeclarationOfUnmodifiedOpinionOrStatementOnImpactOfAuditQualification"] = op
    return filing

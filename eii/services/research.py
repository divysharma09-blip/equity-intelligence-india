"""Builds and refreshes research reports: providers -> verification -> calculations -> scoring."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

import pandas as pd

from eii.calculations import facts_to_frame, key_metrics, momentum
from eii.calculations.financials import KeyMetrics, financial_currency
from eii.models import (DataWarning, FinancialFact, ResearchReport, SecurityIdentity, Source,
                        SourceKind, VerificationStatus, utcnow)
from eii.providers import (FundamentalsBundle, FundamentalsProvider, ProviderRegistry,
                           ProviderUnavailable)
from eii.providers.yahoo import yahoo_ticker
from eii.scoring import (DEFAULT_SCORECARD, ScorecardConfig, assess_readiness, compute_scorecard,
                         infer_company_type)
from eii.valuation import run_scenarios
from eii.verification import verify_identity

log = logging.getLogger("eii.services")

STALE_MONTHS = 15


@dataclass
class BuildResult:
    report: ResearchReport
    price_history: Optional[pd.DataFrame]
    provider_errors: list[str]


def build_report(identity: SecurityIdentity, registry: ProviderRegistry,
                 nse: Optional[pd.DataFrame], bse: Optional[pd.DataFrame],
                 master_sources: list[Source], config: ScorecardConfig = DEFAULT_SCORECARD) -> BuildResult:
    """Query every enabled provider and merge by priority (registry order).

    * Financial statements come from ONE provider — the highest-priority one with facts —
      so definitions and bases are never mixed. Other providers' statements are used only
      to cross-check (reconcile) the chosen figures.
    * Profile, market snapshot, price history, ownership and audit data are taken from the
      first provider that supplies each.
    """
    report = ResearchReport(identity=identity, sources=list(master_sources))
    price_history = None
    errors: list[str] = []
    available = [p for p in registry.fundamentals if p.status().available]
    if not available:
        report.warnings.append(DataWarning(
            severity="warning", section="Financial statements",
            message="No fundamentals data provider is enabled. Financial, valuation and momentum "
                    "sections will be empty until a provider is configured."))
    bundles: list[tuple[FundamentalsProvider, FundamentalsBundle]] = []
    for provider in available:
        try:
            bundles.append((provider, provider.fetch(identity)))
        except ProviderUnavailable as exc:
            log.warning("%s unavailable for %s: %s", provider.name, identity.key, exc)
            errors.append(f"{provider.name}: {exc}")
            report.warnings.append(DataWarning(severity="warning", section="Data providers",
                                               message=f"{provider.name}: {exc}"))

    stmt = next(((p, b) for p, b in bundles if b.facts), None)
    if stmt:
        provider, bundle = stmt
        report.financials = list(bundle.facts)
        report.statement_basis = bundle.statement_basis
        if provider.kind == SourceKind.UNOFFICIAL_AGGREGATOR:
            report.warnings.append(DataWarning(
                severity="warning", section="Financial statements",
                message=f"Financial figures come from {provider.name}, an unofficial aggregator, because "
                        "official filings were unavailable. Cross-check against the annual report."))
    for provider, bundle in bundles:
        is_stmt = stmt is not None and bundle is stmt[1]
        unused_fact_sources = set() if is_stmt else {f.source_id for f in bundle.facts}
        report.sources.extend(s for s in bundle.sources if s.source_id not in unused_fact_sources)
        report.warnings.extend(w for w in bundle.warnings
                               if is_stmt or w.section != "Financial statements")
        report.profile = report.profile or bundle.profile
        report.market = report.market or bundle.market
        report.ownership = report.ownership or bundle.ownership
        report.audit = report.audit or bundle.audit
        if report.company_type == "unknown" and bundle.company_type:
            report.company_type = bundle.company_type
        if price_history is None and bundle.price_history is not None:
            price_history = bundle.price_history
        if not is_stmt and stmt is not None and bundle.facts:
            if report.statement_basis == "Standalone":
                report.warnings.append(DataWarning(
                    severity="info", section="Financial statements",
                    message=f"Not cross-checked against {provider.name}: filed figures are standalone, the "
                            "provider's are typically consolidated."))
            else:
                report.warnings.extend(reconcile(report.financials, bundle.facts, provider.name,
                                                 report.company_type))

    if report.company_type == "unknown" and report.profile:
        inferred = infer_company_type(report.profile.sector, report.profile.industry)
        if inferred != "unknown":
            report.company_type = inferred
            report.warnings.append(DataWarning(
                severity="info", section="Scorecard",
                message=f"Company type '{inferred}' inferred from the data provider's sector "
                        f"('{report.profile.sector} / {report.profile.industry}'), not from filings."))

    if report.market is not None and price_history is not None:
        mom = momentum(price_history)
        report.market = report.market.model_copy(update={k: mom[k] for k in mom})

    report.verification = verify_identity(identity, nse, bse, report.profile,
                                          expected_provider_symbol=yahoo_ticker(identity))
    return BuildResult(refresh_analysis(report, config), price_history, errors)


RECONCILE_TOLERANCE = 0.03


def reconcile(primary: list[FinancialFact], other: list[FinancialFact], other_name: str,
              company_type: str = "industrial") -> list[DataWarning]:
    """Compare chosen statement figures with a second source for the same fiscal years."""
    metrics = ["net_income"] if company_type in ("bank", "nbfc", "insurer") else ["revenue", "net_income"]
    idx = {(f.metric, f.period_end): f for f in other if f.value is not None}
    compared, diffs, unit_mismatch = 0, [], False
    for f in primary:
        if f.metric not in metrics or f.value in (None, 0):
            continue
        o = idx.get((f.metric, f.period_end))
        if o is None:
            continue
        if o.unit != f.unit:
            unit_mismatch = True
            continue
        compared += 1
        gap = abs(o.value - f.value) / abs(f.value)
        if gap > RECONCILE_TOLERANCE:
            diffs.append(f"{f.metric} FY{f.period_end.year}: filed {f.value / 1e7:,.0f} Cr vs "
                         f"{o.value / 1e7:,.0f} Cr ({gap:.0%} apart)")
    if diffs:
        return [DataWarning(severity="warning", section="Financial statements",
                            message=f"Filed figures differ from {other_name} by more than "
                                    f"{RECONCILE_TOLERANCE:.0%}: " + "; ".join(diffs[:6]) +
                                    ". Differences can come from statement basis or restatements; "
                                    "the filed figures are used.")]
    if compared:
        return [DataWarning(severity="info", section="Financial statements",
                            message=f"Cross-checked {compared} filed revenue/profit figures against "
                                    f"{other_name}: all within {RECONCILE_TOLERANCE:.0%}.")]
    if unit_mismatch:
        return [DataWarning(severity="info", section="Financial statements",
                            message=f"Not cross-checked: {other_name} reports this company in a different "
                                    "currency. Filed INR figures are used.")]
    return []


def analysis_warnings(report: ResearchReport, km: KeyMetrics) -> list[DataWarning]:
    out: list[DataWarning] = []

    def add(sev, section, msg):
        out.append(DataWarning(severity=sev, section=section, message=msg, origin="analysis"))

    v = report.verification
    if v is None or v.status == VerificationStatus.UNVERIFIED:
        add("critical", "Identity", "Company identity is not verified against an official exchange list.")
    elif v.status == VerificationStatus.CONFLICT:
        add("critical", "Identity", "Identity checks conflict: " + v.summary)
    elif v.status == VerificationStatus.PARTIAL:
        add("warning", "Identity", "Identity only partially verified: " + v.summary)
    if not report.financials:
        add("warning", "Financial statements", "No financial statement data available.")
    elif km.years_of_data < 3:
        add("warning", "Financial statements",
            f"Only {km.years_of_data} year(s) of revenue data; growth and averages are unreliable.")
    if km.latest_period:
        months = (date.today().year - km.latest_period.year) * 12 + date.today().month - km.latest_period.month
        if months > STALE_MONTHS:
            add("warning", "Financial statements",
                f"Latest annual statements are for the period ending {km.latest_period:%b %Y} "
                f"({months} months ago) and may be stale.")
    if report.market is None or report.market.price is None:
        add("warning", "Valuation", "No current price; valuation multiples and scenarios unavailable.")
    if report.company_type == "bank":
        add("info", "Scorecard", "Bank: industrial ratios (D/E, EV/EBITDA, ROCE, cash conversion…) are replaced "
                                 "by bank metrics (ROA, NII/assets, cost-to-income, credit cost, CET1, GNPA/NNPA).")
    elif report.company_type == "nbfc":
        add("info", "Scorecard", "NBFC: industrial ratios are replaced by ROA, NII/assets, credit cost and "
                                 "leverage. NBFC Stage-3/GNPA and capital adequacy are not in exchange "
                                 "filings, so they are not shown.")
    elif report.company_type == "insurer":
        add("warning", "Scorecard", "Insurer: insurance-specific metrics (VNB, solvency, combined ratio) are not "
                                    "implemented, so most of the scorecard is not applicable.")
    if report.ownership is None:
        add("info", "Governance", "No shareholding / pledge disclosure available.")
    if km.currency_mismatch:
        add("critical", "Currency", km.currency_mismatch)
    g = report.governance
    if g.governance_rating is None:
        add("info", "Governance", "Governance has not been assessed by the analyst.")
    if not report.risks:
        add("info", "Risks", "No key risks recorded.")
    if not report.business_model.strip():
        add("info", "Business model", "Business model section is empty.")
    return out


def refresh_analysis(report: ResearchReport, config: ScorecardConfig = DEFAULT_SCORECARD) -> ResearchReport:
    """Recompute everything derived from data + analyst inputs. Never touches raw facts."""
    km = compute_key_metrics(report)
    scorecard = compute_scorecard(km.values, config, report.company_type)
    price = report.market.price if report.market else None
    eps = None if km.currency_mismatch else km.values.get("latest_eps")
    results, _errors = run_scenarios(price, eps, report.scenarios)
    warnings = [w for w in report.warnings if w.origin == "provider"]
    report = report.model_copy(update={"scorecard": scorecard, "scenario_results": results,
                                       "updated_at": utcnow()})
    warnings += analysis_warnings(report, km)
    readiness = assess_readiness(report.verification.status if report.verification else None,
                                 km.years_of_data, scorecard, bool(price), warnings,
                                 report.company_type)
    return report.model_copy(update={"warnings": warnings, "readiness": readiness})


def compute_key_metrics(report: ResearchReport) -> KeyMetrics:
    return key_metrics(facts_to_frame(report.financials), report.market, report.governance,
                       financial_currency(report.financials) or "INR", report.ownership, report.audit,
                       report.company_type)


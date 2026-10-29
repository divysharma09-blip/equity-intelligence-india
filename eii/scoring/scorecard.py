"""Configurable, transparent research-framework scorecard.

Each metric maps linearly onto 0–100 between a "poor" and an "excellent"
threshold (clipped at both ends). A factor score is the mean of its available
metric scores; the overall score is the weight-normalised mean of factors that
have any data. Coverage reports how much of the framework actually had data.

This is a research framework for organising evidence. It is NOT a validated
predictive model; thresholds are judgement calls documented in the workbook.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from eii.models import (DataWarning, FactorScore, MetricScore, Readiness, ScorecardResult,
                        VerificationStatus)


class MetricRule(BaseModel):
    metric: str
    label: str
    poor: float
    excellent: float
    unit: Literal["pct", "x", "rating", "num", "flag"] = "num"
    applies_to: list[str] = Field(default_factory=lambda: ["all"])  # company types, or "all"

    def applies(self, company_type: str) -> bool:
        return "all" in self.applies_to or company_type in self.applies_to

    @property
    def higher_is_better(self) -> bool:
        return self.excellent > self.poor

    def score(self, value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        frac = (value - self.poor) / (self.excellent - self.poor)
        return round(100 * max(0.0, min(1.0, frac)), 1)

    def describe(self) -> str:
        fmt = (lambda x: f"{x:.1%}") if self.unit == "pct" else (lambda x: f"{x:g}")
        direction = "higher is better" if self.higher_is_better else "lower is better"
        return f"0 at {fmt(self.poor)}, 100 at {fmt(self.excellent)} ({direction})"


class FactorConfig(BaseModel):
    key: str
    label: str
    weight: float = Field(ge=0)
    description: str
    metrics: list[MetricRule]


class ScorecardConfig(BaseModel):
    version: str = "2026.1"
    factors: list[FactorConfig]

    def weights(self) -> dict[str, float]:
        return {f.key: f.weight for f in self.factors}

    def with_weights(self, weights: dict[str, float]) -> "ScorecardConfig":
        return self.model_copy(update={"factors": [
            f.model_copy(update={"weight": float(weights.get(f.key, f.weight))}) for f in self.factors]},
            deep=True)


IND = ["industrial", "unknown"]
FIN = ["bank", "nbfc"]


def _m(metric, label, poor, excellent, unit="num", applies_to=None):
    return MetricRule(metric=metric, label=label, poor=poor, excellent=excellent, unit=unit,
                      applies_to=applies_to or ["all"])


DEFAULT_SCORECARD = ScorecardConfig(version="2026.2", factors=[
    FactorConfig(key="business_quality", label="Business quality", weight=20,
                 description="Returns on capital and margins (industrials); return on assets and "
                             "efficiency (banks/NBFCs).",
                 metrics=[_m("roe_avg_3y", "ROE, 3y avg", 0.08, 0.25, "pct"),
                          _m("roce_avg_3y", "ROCE, 3y avg", 0.08, 0.25, "pct", IND),
                          _m("ebit_margin_avg_3y", "EBIT margin, 3y avg", 0.05, 0.25, "pct", IND),
                          _m("ebit_margin_volatility", "EBIT margin volatility", 0.08, 0.01, "pct", IND),
                          _m("roa_avg_3y", "ROA, 3y avg", 0.005, 0.02, "pct", FIN),
                          _m("nii_to_assets", "NII / assets (NIM proxy)", 0.02, 0.05, "pct", FIN),
                          _m("cost_to_income", "Cost-to-income", 0.65, 0.35, "pct", ["bank"])]),
    FactorConfig(key="growth", label="Growth", weight=15,
                 description="Historical compound growth (up to 3 years).",
                 metrics=[_m("revenue_cagr", "Revenue / total income CAGR", 0.0, 0.20, "pct"),
                          _m("eps_cagr", "EPS CAGR", 0.0, 0.20, "pct"),
                          _m("nii_cagr", "NII CAGR", 0.0, 0.20, "pct", FIN),
                          _m("advances_cagr", "Advances / loans CAGR", 0.0, 0.20, "pct", FIN)]),
    FactorConfig(key="valuation", label="Valuation", weight=15,
                 description="Price paid relative to earnings, book value, EBITDA and free cash flow.",
                 metrics=[_m("pe", "P/E", 60, 10, "x"),
                          _m("pb", "P/B", 10, 1, "x"),
                          _m("ev_ebitda", "EV/EBITDA", 40, 6, "x", IND),
                          _m("fcf_yield", "FCF yield", 0.0, 0.08, "pct", IND)]),
    FactorConfig(key="financial_strength", label="Financial strength", weight=15,
                 description="Leverage, debt service and liquidity (industrials); capital and asset "
                             "quality (banks); balance-sheet leverage (NBFCs).",
                 metrics=[_m("debt_to_equity", "Debt / equity", 2.0, 0.0, "x", IND),
                          _m("interest_coverage", "Interest coverage", 1.5, 15, "x", IND),
                          _m("current_ratio", "Current ratio", 0.8, 2.0, "x", IND),
                          _m("cet1_ratio", "CET1 ratio", 0.08, 0.16, "pct", ["bank"]),
                          _m("gnpa_pct", "Gross NPA ratio", 0.08, 0.01, "pct", ["bank"]),
                          _m("nnpa_pct", "Net NPA ratio", 0.03, 0.0, "pct", ["bank"]),
                          _m("leverage", "Assets / equity", 8.0, 3.0, "x", ["nbfc"])]),
    FactorConfig(key="governance", label="Governance", weight=10,
                 description="Promoter pledge and audit opinion as filed; analyst governance judgement (1–5).",
                 metrics=[_m("governance_rating", "Governance rating (analyst)", 1, 5, "rating"),
                          _m("promoter_pledge_pct", "Promoter shares pledged %", 50, 0, "num"),
                          _m("audit_opinion_clean", "Unmodified audit opinion (filed)", 0, 1, "flag")]),
    FactorConfig(key="earnings_quality", label="Earnings quality", weight=10,
                 description="Cash backing of profits (industrials); credit cost (banks/NBFCs).",
                 metrics=[_m("cash_conversion_3y", "OCF / net profit, 3y", 0.5, 1.1, "x", IND),
                          _m("accruals_ratio", "Accruals ratio", 0.10, -0.05, "pct", IND),
                          _m("credit_cost", "Credit cost (provisions / loans)", 0.03, 0.005, "pct", FIN)]),
    FactorConfig(key="macro_resilience", label="Macroeconomic resilience", weight=5,
                 description="Analyst judgement (1–5) of sensitivity to rates, commodities, FX and the cycle.",
                 metrics=[_m("macro_resilience_rating", "Macro resilience (analyst)", 1, 5, "rating")]),
    FactorConfig(key="momentum", label="Momentum", weight=10,
                 description="Trailing price performance and trend.",
                 metrics=[_m("return_12m", "12-month return", -0.30, 0.40, "pct"),
                          _m("price_vs_200dma", "Price vs 200-day average", -0.20, 0.20, "pct")]),
])

COMPANY_TYPE_LABELS = {"industrial": "Industrial / non-financial", "bank": "Bank", "nbfc": "NBFC",
                       "insurer": "Insurer", "unknown": "Unclassified (treated as non-financial)"}


def is_financial_sector(sector: Optional[str], industry: Optional[str] = None) -> bool:
    text = f"{sector or ''} {industry or ''}".lower()
    return "financial services" in text or any(w in text for w in ("bank", "insurance", "credit services"))


def infer_company_type(sector: Optional[str], industry: Optional[str]) -> str:
    """Fallback classification from a provider's sector text (filings format is preferred)."""
    text = f"{sector or ''} {industry or ''}".lower()
    if "bank" in text:
        return "bank"
    if "insurance" in text:
        return "insurer"
    if "financial services" in text or "credit services" in text:
        return "nbfc"
    return "industrial" if text.strip() else "unknown"


def compute_scorecard(values: dict[str, Optional[float]], config: ScorecardConfig = DEFAULT_SCORECARD,
                      company_type: str = "unknown") -> ScorecardResult:
    """Metrics that do not apply to the company type are listed but excluded from coverage."""
    factors: list[FactorScore] = []
    for fc in config.factors:
        metric_scores, applicable = [], 0
        for rule in fc.metrics:
            if rule.applies(company_type):
                applicable += 1
                value = values.get(rule.metric)
                metric_scores.append(MetricScore(metric=rule.metric, label=rule.label, value=value,
                                                 score=rule.score(value), rule=rule.describe()))
            else:
                metric_scores.append(MetricScore(
                    metric=rule.metric, label=rule.label, value=None, score=None, rule=rule.describe(),
                    excluded_reason=f"Not applicable to {COMPANY_TYPE_LABELS.get(company_type, company_type)}."))
        available = [m.score for m in metric_scores if m.score is not None]
        factors.append(FactorScore(
            factor=fc.key, label=fc.label, weight=fc.weight,
            score=round(sum(available) / len(available), 1) if available else None,
            coverage=len(available) / applicable if applicable else 0.0,
            metrics=metric_scores))
    total_w = sum(f.weight for f in factors)
    scored = [f for f in factors if f.score is not None and f.weight > 0]
    scored_w = sum(f.weight for f in scored)
    overall = round(sum(f.score * f.weight for f in scored) / scored_w, 1) if scored_w else None
    coverage = sum(f.coverage * f.weight for f in factors) / total_w if total_w else 0.0
    return ScorecardResult(overall_score=overall, coverage=round(coverage, 3), factors=factors)


MIN_COVERAGE = 0.6
MIN_YEARS = 3


UNSUPPORTED_TYPES = {"insurer"}


def assess_readiness(verification: Optional[VerificationStatus], years_of_data: int,
                     scorecard: Optional[ScorecardResult], has_price: bool,
                     warnings: list[DataWarning], company_type: str = "unknown") -> Readiness:
    """Gate that decides whether ANY framework conclusion may be displayed."""
    reasons = []
    if company_type in UNSUPPORTED_TYPES:
        reasons.append(f"Sector-specific metrics for {COMPANY_TYPE_LABELS[company_type].lower()}s are not "
                       "implemented, so the framework cannot assess this company.")
    if verification != VerificationStatus.VERIFIED:
        reasons.append(f"Company identity is {verification.value if verification else 'unverified'}, "
                       "not fully verified against the exchange list.")
    if years_of_data < MIN_YEARS:
        reasons.append(f"Only {years_of_data} year(s) of financial statements; at least {MIN_YEARS} required.")
    if scorecard is None or scorecard.coverage < MIN_COVERAGE:
        cov = scorecard.coverage if scorecard else 0
        reasons.append(f"Scorecard data coverage is {cov:.0%}; at least {MIN_COVERAGE:.0%} required.")
    if not has_price:
        reasons.append("No current market price.")
    crit = [w for w in warnings if w.severity == "critical"]
    if crit:
        reasons.append(f"{len(crit)} critical data-quality warning(s) unresolved.")
    return Readiness(can_conclude=not reasons, reasons=reasons)


def score_band(score: Optional[float]) -> str:
    if score is None:
        return "Not scored"
    if score >= 75:
        return "Strong on this framework"
    if score >= 60:
        return "Above average on this framework"
    if score >= 45:
        return "Mixed on this framework"
    return "Weak on this framework"

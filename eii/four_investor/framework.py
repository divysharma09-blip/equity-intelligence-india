"""Four-investor framework (Buffett, Munger, Dalio, Fisher): parameters, weights and scoring.

Every parameter is rated 0–4 (critical failure … excellent) and earns rating / 4 × weight.
Ratings come from two places:

* the app, for parameters a filed metric measures directly (rules below, thresholds per
  company type) — these are deterministic and always win;
* a saved Claude Code research run (see `eii.four_investor.claude_run`) for the qualitative rest.

Unrated parameters are excluded, never scored as zero: an investor's score is points earned over
the weight actually rated, and coverage reports how much of the 100 points that was.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from pydantic import BaseModel

from eii.calculations.financials import KeyMetrics

INVESTORS = {"buffett": "Warren Buffett", "munger": "Charlie Munger", "dalio": "Ray Dalio",
             "fisher": "Philip Fisher"}
RATING_LABELS = {0: "Critical failure", 1: "Poor", 2: "Below average", 3: "Good", 4: "Excellent"}
MIN_COVERAGE = 0.6

IND = ("industrial", "unknown")
FIN = ("bank", "nbfc")


@dataclass(frozen=True)
class Param:
    id: str
    investor: str
    category: str
    label: str
    weight: int


def _p(investor: str, cat_key: str, category: str, *items: tuple[str, str, int]) -> list[Param]:
    return [Param(f"{investor}.{cat_key}.{key}", investor, category, label, w) for key, label, w in items]


PARAMS: list[Param] = [
    *_p("buffett", "moat", "Economic moat",
        ("brand", "Brand strength", 5), ("pricing_power", "Pricing power", 5),
        ("barriers", "Competitive advantage and barriers to entry", 7),
        ("share_loyalty", "Market share stability and customer loyalty", 5),
        ("cost_network", "Cost advantage or network effects", 3)),
    *_p("buffett", "economics", "Business economics",
        ("roce", "ROCE consistency", 7), ("roe", "ROE consistency", 5),
        ("margin_stability", "Operating margin stability", 5),
        ("incremental_roc", "Incremental return on capital", 5),
        ("predictability", "Earnings predictability", 3)),
    *_p("buffett", "strength", "Financial strength and cash generation",
        ("fcf", "Free cash flow generation", 7), ("cfo_pat", "Operating cash flow versus PAT", 5),
        ("leverage", "Debt and leverage", 5), ("interest_cover", "Interest coverage", 4),
        ("working_capital", "Cash conversion and working capital", 4)),
    *_p("buffett", "value", "Intrinsic value and margin of safety",
        ("dcf", "DCF intrinsic value", 8), ("owner_earnings", "Owner earnings valuation", 5),
        ("historical_valuation", "Historical valuation comparison", 4),
        ("margin_of_safety", "Margin of safety", 8)),

    *_p("munger", "quality", "Business quality and simplicity",
        ("understandable", "Business model understandability", 5),
        ("revenue_predictability", "Revenue predictability", 5),
        ("earnings_consistency", "Earnings consistency", 7),
        ("industry_durability", "Industry durability", 5),
        ("complexity", "Operational complexity", 3)),
    *_p("munger", "integrity", "Management integrity",
        ("track_record", "Promoter and management track record", 5),
        ("related_party", "Related-party transactions", 5),
        ("auditor", "Auditor qualifications or resignations", 5),
        ("pledging", "Promoter pledging and governance", 5),
        ("transparency", "Transparency and disclosure quality", 5)),
    *_p("munger", "capital", "Capital allocation",
        ("reinvested_returns", "Return on reinvested capital", 7),
        ("acquisitions", "Acquisition discipline", 5),
        ("dividends_buybacks", "Dividend and buyback decisions", 4),
        ("debt_management", "Debt management", 5), ("dilution", "Share dilution control", 4)),
    *_p("munger", "protection", "Permanent capital-loss protection",
        ("balance_sheet", "Balance-sheet resilience", 7),
        ("downside", "Earnings downside resilience", 5),
        ("disruption", "Competitive disruption risk", 5),
        ("accounting", "Accounting and fraud risk", 5),
        ("survival", "Business survival under stress", 3)),

    *_p("dalio", "cycle", "Economic-cycle positioning",
        ("gdp", "Sensitivity to GDP growth", 6), ("recession", "Recession resistance", 7),
        ("cyclicality", "Demand cyclicality", 6),
        ("cycle_position", "Industry position in economic cycles", 6)),
    *_p("dalio", "rates", "Interest rates and inflation",
        ("rate_sensitivity", "Interest-rate sensitivity", 7),
        ("inflation_margins", "Inflation impact on margins", 6),
        ("input_costs", "Input-cost sensitivity", 6), ("fx", "Foreign-exchange exposure", 6)),
    *_p("dalio", "credit", "Credit and debt conditions",
        ("refinancing", "Debt maturity and refinancing risk", 7),
        ("interest_cover", "Interest coverage", 5), ("liquidity", "Liquidity position", 5),
        ("credit_dependence", "Credit availability dependence", 4),
        ("stress", "Balance-sheet stress resilience", 4)),
    *_p("dalio", "macro", "Macro and geopolitical resilience",
        ("commodity", "Commodity price exposure", 6),
        ("international", "International revenue exposure", 5),
        ("supply_chain", "Supply-chain vulnerability", 5),
        ("government", "Government and regulatory dependence", 5),
        ("geopolitical", "Geopolitical exposure", 4)),

    *_p("fisher", "growth", "Growth potential",
        ("revenue_cagr", "Revenue CAGR (3–5 years)", 6), ("eps_cagr", "EPS CAGR (3–5 years)", 6),
        ("tam", "Addressable market growth", 5),
        ("penetration", "Market penetration opportunity", 4),
        ("demand_visibility", "Future demand visibility", 4)),
    *_p("fisher", "innovation", "Innovation and R&D",
        ("rnd", "R&D expenditure and efficiency", 5),
        ("new_products", "New product development", 5),
        ("patents", "Patent and technology strength", 5),
        ("new_product_revenue", "Revenue contribution from new products", 5),
        ("adaptability", "Ability to adapt to industry changes", 5)),
    *_p("fisher", "competition", "Competitive position",
        ("share_growth", "Market share growth", 7), ("retention", "Customer retention", 5),
        ("distribution", "Distribution strength", 5),
        ("differentiation", "Product differentiation", 5),
        ("positioning", "Competitive positioning", 3)),
    *_p("fisher", "execution", "Management and execution",
        ("vision", "Management vision", 5),
        ("execution", "Execution against stated targets", 7),
        ("talent", "Employee and talent development", 4),
        ("efficiency", "Operational efficiency", 4),
        ("reinvestment", "Reinvestment for long-term growth", 5)),
]
PARAM_BY_ID = {p.id: p for p in PARAMS}


def _check_weights() -> None:
    totals: dict[tuple[str, str], int] = {}
    for p in PARAMS:
        totals[(p.investor, p.category)] = totals.get((p.investor, p.category), 0) + p.weight
    bad = {k: v for k, v in totals.items() if v != 25}
    assert not bad and len(totals) == 16, f"Category weights must each be 25: {bad}"


_check_weights()


# ---------------------------------------------------------------------------- app ratings

@dataclass(frozen=True)
class Rule:
    """Rating = number of cut points the value clears (0–4). Cuts run from poor to excellent."""
    param_id: str
    metric: str
    cuts: tuple[float, float, float, float]
    unit: Literal["pct", "x", "num", "flag"]
    applies_to: tuple[str, ...] = IND
    note: str = ""

    @property
    def higher_is_better(self) -> bool:
        return self.cuts[-1] > self.cuts[0]

    def rate(self, value: float) -> int:
        if self.higher_is_better:
            return sum(value >= c for c in self.cuts)
        return sum(value <= c for c in self.cuts)


RULES: list[Rule] = [
    Rule("buffett.economics.roce", "roce_avg_3y", (0.06, 0.10, 0.15, 0.20), "pct"),
    Rule("buffett.economics.roce", "roa_avg_3y", (0.004, 0.008, 0.012, 0.018), "pct", FIN,
         "Lender substitute: ROA, 3y average."),
    Rule("buffett.economics.roe", "roe_avg_3y", (0.06, 0.10, 0.15, 0.20), "pct", IND + FIN),
    Rule("buffett.economics.margin_stability", "ebit_margin_volatility", (0.10, 0.06, 0.03, 0.015), "pct",
         note="Standard deviation of EBIT margin across filed years."),
    Rule("buffett.strength.fcf", "fcf_margin", (0.0, 0.02, 0.05, 0.10), "pct",
         note="Latest free cash flow / revenue."),
    Rule("buffett.strength.cfo_pat", "cash_conversion_3y", (0.5, 0.7, 0.9, 1.1), "x"),
    Rule("buffett.strength.leverage", "debt_to_equity", (2.0, 1.0, 0.5, 0.2), "x"),
    Rule("buffett.strength.leverage", "leverage", (10, 8, 6, 4), "x", ("nbfc",),
         "NBFC substitute: assets / equity."),
    Rule("buffett.strength.interest_cover", "interest_coverage", (1.5, 3, 6, 12), "x"),
    Rule("buffett.strength.working_capital", "current_ratio", (0.8, 1.0, 1.3, 1.8), "x"),
    Rule("buffett.value.owner_earnings", "fcf_yield", (0.0, 0.02, 0.04, 0.07), "pct",
         note="Proxy: free-cash-flow yield (owner earnings need maintenance capex, which filings don't split)."),
    Rule("munger.quality.earnings_consistency", "ebit_margin_volatility", (0.10, 0.06, 0.03, 0.015), "pct",
         note="Standard deviation of EBIT margin across filed years."),
    Rule("munger.integrity.pledging", "promoter_pledge_pct", (50, 20, 5, 0.5), "num", IND + FIN + ("insurer",),
         "Promoter shares pledged, % of promoter holding, as filed with NSE."),
    Rule("munger.capital.debt_management", "debt_to_equity", (2.0, 1.0, 0.5, 0.2), "x"),
    Rule("munger.protection.balance_sheet", "interest_coverage", (1.5, 3, 6, 12), "x",
         note="Interest coverage as the balance-sheet stress measure."),
    Rule("munger.protection.accounting", "accruals_ratio", (0.10, 0.06, 0.02, -0.02), "pct",
         note="Accruals ratio only; auditor and related-party checks are separate parameters."),
    Rule("dalio.credit.interest_cover", "interest_coverage", (1.5, 3, 6, 12), "x"),
    Rule("dalio.credit.liquidity", "current_ratio", (0.8, 1.0, 1.3, 1.8), "x"),
    Rule("dalio.credit.stress", "debt_to_equity", (2.0, 1.0, 0.5, 0.2), "x"),
    Rule("fisher.growth.revenue_cagr", "revenue_cagr", (0.0, 0.05, 0.10, 0.15), "pct", IND + FIN),
    Rule("fisher.growth.eps_cagr", "eps_cagr", (0.0, 0.05, 0.10, 0.15), "pct", IND + FIN),
    Rule("fisher.execution.efficiency", "ebit_margin_avg_3y", (0.03, 0.07, 0.12, 0.18), "pct",
         note="EBIT margin, 3y average."),
]
_unknown = {r.param_id for r in RULES} - PARAM_BY_ID.keys()
assert not _unknown, f"Rules reference unknown parameters: {_unknown}"


Source = Literal["app", "claude"]
Confidence = Literal["High", "Medium", "Low"]


class Rating(BaseModel):
    param_id: str
    rating: Optional[int] = None  # 0–4; None = unavailable
    observed: str = ""
    evidence: str = ""
    confidence: Optional[Confidence] = None
    source: Source = "app"


def _fmt(value: float, unit: str) -> str:
    return {"pct": f"{value:.1%}", "x": f"{value:.2f}x", "flag": "yes" if value else "no"}.get(unit, f"{value:g}")


def app_ratings(km: KeyMetrics, company_type: str) -> dict[str, Rating]:
    """Ratings the app can justify from filed data alone."""
    v = dict(km.values)
    rev, fcf = v.get("latest_revenue"), v.get("latest_fcf")
    v["fcf_margin"] = fcf / rev if fcf is not None and rev and rev > 0 else None
    out: dict[str, Rating] = {}
    for rule in RULES:
        if rule.param_id in out or company_type not in rule.applies_to:
            continue
        value = v.get(rule.metric)
        if value is None:
            continue
        cuts = " / ".join(_fmt(c, rule.unit) for c in rule.cuts)
        out[rule.param_id] = Rating(
            param_id=rule.param_id, rating=rule.rate(value), observed=_fmt(value, rule.unit),
            evidence=" ".join(x for x in [rule.note, km.notes.get(rule.metric, ""),
                                          f"Cut points for ratings 1–4: {cuts}."] if x),
            confidence="High", source="app")
    if v.get("audit_opinion_clean") is not None and company_type in IND + FIN:
        clean = bool(v["audit_opinion_clean"])
        out["munger.integrity.auditor"] = Rating(
            param_id="munger.integrity.auditor", rating=3 if clean else 1,
            observed="Unmodified opinion" if clean else "Modified opinion",
            evidence="Audit opinion as filed with NSE. Capped at 3 because auditor resignations are not checked "
                     "by the app.", confidence="High", source="app")
    return out


# ---------------------------------------------------------------------------- scoring

@dataclass
class CategoryScore:
    category: str
    points: float
    rated_weight: int


@dataclass
class InvestorScore:
    investor: str
    score: Optional[float]  # 0–100 over the rated weight
    coverage: float  # rated weight / 100
    categories: list[CategoryScore]

    @property
    def provisional(self) -> bool:
        return self.coverage < MIN_COVERAGE


def merge(app: dict[str, Rating], claude: dict[str, Rating]) -> dict[str, Rating]:
    """App ratings win; Claude's fill only the parameters the app could not rate."""
    return {**{k: r for k, r in claude.items() if r.rating is not None}, **app}


def score_investor(investor: str, ratings: dict[str, Rating]) -> InvestorScore:
    cats: dict[str, CategoryScore] = {}
    for p in PARAMS:
        if p.investor != investor:
            continue
        c = cats.setdefault(p.category, CategoryScore(p.category, 0.0, 0))
        r = ratings.get(p.id)
        if r is not None and r.rating is not None:
            c.points += r.rating / 4 * p.weight
            c.rated_weight += p.weight
    rated = sum(c.rated_weight for c in cats.values())
    earned = sum(c.points for c in cats.values())
    return InvestorScore(investor, round(100 * earned / rated, 1) if rated else None, rated / 100,
                         list(cats.values()))


def combined_score(scores: list[InvestorScore]) -> Optional[float]:
    """Equal-weighted mean of the four investor scores; None unless all four have a score."""
    vals = [s.score for s in scores]
    return round(sum(vals) / len(vals), 1) if vals and all(v is not None for v in vals) else None


def score_band(score: Optional[float]) -> str:
    if score is None:
        return "—"
    for floor, label in ((90, "Exceptional"), (80, "Excellent"), (70, "Good"), (60, "Average"),
                         (50, "Weak"), (30, "Poor")):
        if score >= floor:
            return label
    return "Critical"

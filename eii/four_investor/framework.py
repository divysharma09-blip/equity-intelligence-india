"""Four-investor framework (Buffett, Munger, Dalio, Fisher): parameters, weights and scoring.

Every parameter is rated 0–4 (critical failure … excellent) and earns rating / 4 × weight.
Ratings come from three places, in order of precedence:

1. the app, from filed statements, BRSR, announcements, shareholding and prices (`app_ratings`,
   rules below, thresholds per company type) — deterministic, always win;
2. a saved Claude Code research run (`eii.four_investor.claude_run`) — company-specific research;
3. the app's mechanical DCF ("model") and the company's sector profile (`sector_profiles`) —
   assumption-driven or industry-level, so company research replaces them.

A parameter can also be not applicable (patents for a power utility, FCF for a bank). Those are
excluded from the denominator, as the master prompt allows. Unrated parameters are excluded from
the score but count against coverage: score = points earned / weight rated, coverage = weight
rated / weight applicable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Optional

import pandas as pd
from pydantic import BaseModel

from eii.calculations.financials import KeyMetrics
from eii.four_investor.sector_profiles import (INNOVATION_PARAMS, PATENT_SECTORS, PROFILE_PARAMS, PROFILES,
                                               SectorProfile,
                                               resolve_sector)
from eii.valuation import dcf_value

if TYPE_CHECKING:
    from eii.four_investor.evidence import Evidence

INVESTORS = {"buffett": "Warren Buffett", "munger": "Charlie Munger", "dalio": "Ray Dalio",
             "fisher": "Philip Fisher"}
RATING_LABELS = {0: "Critical failure", 1: "Poor", 2: "Below average", 3: "Good", 4: "Excellent"}
MIN_COVERAGE = 0.6

IND = ("industrial", "unknown")
FIN = ("bank", "nbfc")
ALL = IND + FIN + ("insurer",)
NOMINAL_GDP_GROWTH = 0.10  # India, FY24 9.6%, FY25 9.8%, FY26 ~8.9% (MoSPI); long-run benchmark
DCF_DISCOUNT_RATE = 0.12  # ~6.5% 10-year G-sec + ~6% equity risk premium
DCF_TERMINAL_GROWTH = 0.05  # nominal, below long-run nominal GDP


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


GROWTH_VOL = (0.30, 0.20, 0.10, 0.05)
RULES: list[Rule] = [
    # --- filed statements (key metrics)
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
    Rule("buffett.strength.leverage", "cet1_ratio", (0.09, 0.11, 0.13, 0.15), "pct", ("bank",),
         "Bank substitute: CET1 capital ratio."),
    Rule("buffett.strength.interest_cover", "interest_coverage", (1.5, 3, 6, 12), "x"),
    Rule("buffett.strength.working_capital", "current_ratio", (0.8, 1.0, 1.3, 1.8), "x"),
    Rule("buffett.value.owner_earnings", "fcf_yield", (0.0, 0.02, 0.04, 0.07), "pct",
         note="Proxy: free-cash-flow yield (owner earnings need maintenance capex, which filings don't split)."),
    Rule("munger.quality.earnings_consistency", "ebit_margin_volatility", (0.10, 0.06, 0.03, 0.015), "pct",
         note="Standard deviation of EBIT margin across filed years."),
    Rule("munger.quality.earnings_consistency", "roa_volatility", (0.010, 0.006, 0.003, 0.0015), "pct", FIN,
         "Lender substitute: standard deviation of ROA across filed years."),
    Rule("munger.integrity.pledging", "promoter_pledge_pct", (50, 20, 5, 0.5), "num", ALL,
         "Promoter shares pledged, % of promoter holding, as filed with NSE."),
    Rule("munger.capital.debt_management", "debt_to_equity", (2.0, 1.0, 0.5, 0.2), "x"),
    Rule("munger.capital.debt_management", "leverage", (10, 8, 6, 4), "x", ("nbfc",), "NBFC: assets / equity."),
    Rule("munger.protection.balance_sheet", "interest_coverage", (1.5, 3, 6, 12), "x",
         note="Interest coverage as the balance-sheet stress measure."),
    Rule("munger.protection.balance_sheet", "gnpa_pct", (0.08, 0.05, 0.03, 0.015), "pct", FIN,
         "Lender substitute: gross NPA ratio."),
    Rule("munger.protection.accounting", "accruals_ratio", (0.10, 0.06, 0.02, -0.02), "pct",
         note="Accruals ratio only; auditor and related-party checks are separate parameters."),
    Rule("munger.protection.downside", "credit_cost", (0.03, 0.02, 0.01, 0.005), "pct", FIN,
         "Lender substitute: credit cost (provisions / loans)."),
    Rule("dalio.credit.interest_cover", "interest_coverage", (1.5, 3, 6, 12), "x"),
    Rule("dalio.credit.liquidity", "current_ratio", (0.8, 1.0, 1.3, 1.8), "x"),
    Rule("dalio.credit.stress", "debt_to_equity", (2.0, 1.0, 0.5, 0.2), "x"),
    Rule("dalio.credit.stress", "nnpa_pct", (0.03, 0.02, 0.01, 0.005), "pct", FIN,
         "Lender substitute: net NPA ratio."),
    Rule("fisher.growth.revenue_cagr", "revenue_cagr", (0.0, 0.05, 0.10, 0.15), "pct", IND + FIN),
    Rule("fisher.growth.eps_cagr", "eps_cagr", (0.0, 0.05, 0.10, 0.15), "pct", IND + FIN),
    Rule("fisher.execution.efficiency", "ebit_margin_avg_3y", (0.03, 0.07, 0.12, 0.18), "pct",
         note="EBIT margin, 3y average."),
    Rule("fisher.execution.efficiency", "cost_to_income", (0.65, 0.55, 0.45, 0.35), "pct", FIN,
         "Lender substitute: cost-to-income ratio."),
    # --- multi-year derivations from the filed statements (`derived_metrics`)
    Rule("buffett.economics.incremental_roc", "incremental_roce", (0.05, 0.10, 0.15, 0.20), "pct",
         note="Change in EBIT / change in capital employed (equity + debt) over the last 3 years."),
    Rule("buffett.economics.predictability", "loss_years", (3, 2, 1, 0), "num", IND + FIN,
         note="Loss-making years in the last 5 filed years."),
    Rule("buffett.moat.barriers", "excess_roce_share", (0.2, 0.4, 0.6, 0.8), "pct",
         note="Share of filed years with ROCE ≥ 15% (above India's ~12% cost of capital). Sustained excess "
              "returns are the financial footprint of a moat."),
    Rule("buffett.moat.share_loyalty", "revenue_growth_volatility", GROWTH_VOL, "pct", IND + FIN,
         note="Proxy: stability of year-on-year revenue growth (loyal customers give steady revenue)."),
    Rule("buffett.moat.pricing_power", "gross_margin_volatility", (0.08, 0.05, 0.03, 0.015), "pct",
         note="Standard deviation of gross margin (after material and traded-goods cost) across filed years: "
              "a firm with pricing power passes input-cost swings on."),
    Rule("munger.quality.revenue_predictability", "revenue_growth_volatility", GROWTH_VOL, "pct", IND + FIN,
         note="Standard deviation of year-on-year revenue growth."),
    Rule("munger.capital.reinvested_returns", "incremental_roce", (0.05, 0.10, 0.15, 0.20), "pct",
         note="Change in EBIT / change in capital employed (equity + debt) over the last 3 years."),
    Rule("munger.capital.dilution", "share_count_cagr", (0.08, 0.04, 0.02, 0.005), "pct", ALL,
         note="Annual growth in shares outstanding (split/bonus adjusted). Convertible instruments not yet "
              "converted are not counted."),
    Rule("munger.protection.downside", "worst_ebit_change", (-0.60, -0.35, -0.20, -0.05), "pct",
         note="Worst year-on-year change in EBIT across filed years."),
    Rule("dalio.cycle.recession", "covid_revenue_change", (-0.30, -0.15, -0.05, 0.0), "pct", IND + FIN,
         note="Revenue change in FY21 (the COVID recession year) vs FY20."),
    Rule("dalio.cycle.cyclicality", "revenue_growth_volatility", GROWTH_VOL, "pct", IND + FIN,
         note="Standard deviation of year-on-year revenue growth."),
    Rule("dalio.rates.rate_sensitivity", "interest_burden", (0.60, 0.40, 0.25, 0.10), "pct",
         note="Latest interest expense / EBIT: the share of operating profit a rate rise acts on."),
    Rule("dalio.rates.inflation_margins", "inflation_margin_change", (-0.06, -0.03, -0.01, 0.0), "pct",
         note="Change in EBIT margin in FY23 (India's input-cost inflation peak) vs FY22."),
    Rule("dalio.rates.input_costs", "material_cost_share", (0.70, 0.55, 0.40, 0.25), "pct",
         note="Material and traded-goods cost / revenue, 3y average: the exposed share of costs."),
    Rule("dalio.credit.credit_dependence", "net_debt_to_ebitda", (5.0, 3.0, 2.0, 1.0), "x",
         note="Latest (debt − cash) / EBITDA."),
    Rule("dalio.credit.refinancing", "short_term_debt_share", (0.60, 0.40, 0.25, 0.10), "pct",
         note="Short-term borrowings / total borrowings: debt that must be refinanced within a year."),
    Rule("fisher.execution.reinvestment", "capex_to_depreciation", (0.5, 0.8, 1.0, 1.5), "x",
         note="Capex / depreciation, 3y total: above 1x means the asset base is being grown, not just kept."),
    Rule("fisher.execution.execution", "growth_delivery", (0.2, 0.4, 0.6, 0.8), "pct", IND + FIN,
         note="Proxy for delivery: share of filed years in which both revenue and operating profit grew."),
    Rule("fisher.competition.share_growth", "growth_vs_gdp", (-0.05, 0.0, 0.03, 0.07), "pct", IND + FIN,
         note=f"Revenue CAGR minus India's nominal GDP growth (~{NOMINAL_GDP_GROWTH:.0%}): growing faster than "
              "the economy implies share gains or a faster-growing niche."),
    Rule("fisher.competition.differentiation", "gross_margin", (0.20, 0.30, 0.40, 0.50), "pct",
         note="Gross margin, latest year: differentiated products command a premium over input cost."),
    Rule("fisher.competition.differentiation", "services_ebit_margin", (0.08, 0.12, 0.18, 0.25), "pct",
         note="Services substitute (no material cost): EBIT margin, 3y average."),
    Rule("buffett.moat.pricing_power", "services_margin_volatility", (0.08, 0.05, 0.03, 0.015), "pct",
         note="Services substitute (no material cost): standard deviation of EBIT margin."),
    Rule("dalio.rates.input_costs", "employee_cost_share", (0.70, 0.55, 0.45, 0.35), "pct",
         note="Services substitute: employee cost / revenue, 3y average (wages are the main input cost)."),
    Rule("buffett.moat.barriers", "excess_roe_share", (0.2, 0.4, 0.6, 0.8), "pct", FIN,
         "Lender substitute: share of filed years with ROE ≥ 15%."),
    Rule("munger.protection.survival", "cet1_ratio", (0.09, 0.11, 0.13, 0.15), "pct", ("bank",),
         "Bank substitute: CET1 capital ratio."),
    Rule("munger.protection.survival", "leverage", (10, 8, 6, 4), "x", ("nbfc",), "NBFC substitute: assets / equity."),
    Rule("fisher.competition.distribution", "locations_national", (20, 100, 500, 2000), "num", FIN,
         "Lender substitute: offices and branches in India (BRSR)."),
    Rule("dalio.rates.rate_sensitivity", "nii_to_assets", (0.02, 0.025, 0.03, 0.04), "pct", FIN,
         "Lender substitute: net interest income / assets — a wider margin absorbs rate moves."),
    Rule("dalio.credit.stress", "leverage", (10, 8, 6, 4), "x", ("nbfc",),
         "NBFC substitute: assets / equity (NBFC NPAs are not in the XBRL filing)."),
    Rule("munger.protection.balance_sheet", "leverage", (10, 8, 6, 4), "x", ("nbfc",),
         "NBFC substitute: assets / equity (NBFC NPAs are not in the XBRL filing)."),
    Rule("munger.capital.debt_management", "cet1_ratio", (0.09, 0.11, 0.13, 0.15), "pct", ("bank",),
         "Bank substitute: CET1 capital ratio (deposits are a bank's raw material, not debt to manage down)."),
    # --- automatic DCF (`dcf_estimates`)
    Rule("buffett.value.dcf", "dcf_upside", (-0.30, -0.10, 0.10, 0.30), "pct",
         note="Base-case DCF value per share vs price."),
    Rule("buffett.value.margin_of_safety", "bear_dcf_upside", (-0.40, -0.20, 0.0, 0.25), "pct",
         note="Bear-case DCF value per share vs price: the margin of safety if things go worse."),
    # --- public evidence (`evidence.collect`)
    Rule("buffett.value.historical_valuation", "pe_vs_history", (1.5, 1.2, 1.0, 0.8), "x", IND + FIN),
    Rule("buffett.moat.cost_network", "index_tier", (1, 2, 3, 4), "num", ALL,
         "Proxy for scale economies: NSE index tier (4 = Nifty 100, 3 = Midcap 150, 2 = Smallcap 250, "
         "1 = Microcap 250, 0 = outside the 750 largest)."),
    Rule("fisher.competition.positioning", "index_tier", (1, 2, 3, 4), "num", ALL,
         "Proxy: scale among listed peers by NSE index tier (4 = Nifty 100 … 0 = outside the 750 largest)."),
    Rule("munger.integrity.related_party", "rpt_max_share", (0.50, 0.25, 0.10, 0.02), "pct", ALL,
         "Largest related-party share of purchases, sales, loans or investments (BRSR)."),
    Rule("munger.integrity.transparency", "investor_comms_12m", (0, 2, 4, 6), "num", ALL,
         "Investor presentations, earnings-call notices and transcripts filed with NSE in the last 12 months."),
    Rule("dalio.rates.fx", "exports_pct", (0.50, 0.30, 0.15, 0.05), "pct", ALL,
         "Exports as % of turnover (BRSR); import dependence is not disclosed and is covered in the sector profile."),
    Rule("dalio.macro.international", "exports_pct", (0.50, 0.30, 0.15, 0.05), "pct", ALL,
         "Exports as % of turnover (BRSR)."),
    Rule("fisher.execution.talent", "attrition", (0.30, 0.20, 0.12, 0.06), "pct", ALL,
         "Permanent-employee turnover rate (BRSR)."),
]
_unknown = ({r.param_id for r in RULES} | set(PROFILE_PARAMS) | set(INNOVATION_PARAMS)) - PARAM_BY_ID.keys()
assert not _unknown, f"Rules or sector profiles reference unknown parameters: {_unknown}"

# Assumption-driven valuations: a research run's company-specific valuation replaces them.
MODEL_PARAMS = ("buffett.value.dcf", "buffett.value.margin_of_safety")

# Not meaningful for lenders: substitutes above where one exists, excluded otherwise.
LENDER_NOT_APPLICABLE = (
    "buffett.strength.fcf", "buffett.strength.cfo_pat", "buffett.strength.interest_cover",
    "buffett.strength.working_capital", "buffett.economics.margin_stability", "buffett.economics.incremental_roc",
    "buffett.moat.pricing_power", "buffett.value.dcf", "buffett.value.owner_earnings",
    "buffett.value.margin_of_safety", "munger.capital.reinvested_returns", "munger.protection.accounting",
    "dalio.rates.input_costs", "dalio.rates.inflation_margins", "dalio.credit.interest_cover",
    "dalio.credit.liquidity", "dalio.credit.refinancing", "dalio.credit.credit_dependence",
    "fisher.competition.differentiation", "fisher.execution.reinvestment",
)


Source = Literal["app", "model", "claude", "sector"]
Confidence = Literal["High", "Medium", "Low"]


class Rating(BaseModel):
    param_id: str
    rating: Optional[int] = None  # 0–4; None = unavailable
    applicable: bool = True  # False = excluded from coverage (not meaningful for this company)
    observed: str = ""
    evidence: str = ""
    confidence: Optional[Confidence] = None
    source: Source = "app"


def _na(param_id: str, why: str, source: Source = "app") -> Rating:
    return Rating(param_id=param_id, applicable=False, observed="Not applicable", evidence=why,
                  source=source)


def _fmt(value: float, unit: str) -> str:
    return {"pct": f"{value:.1%}", "x": f"{value:.2f}x", "flag": "yes" if value else "no"}.get(unit, f"{value:g}")


def _col(fin: pd.DataFrame, name: str) -> pd.Series:
    return fin[name].astype(float).dropna() if name in fin.columns else pd.Series(dtype=float)


def _fy(fin_index, year: int):
    hits = [d for d in fin_index if d.year == year and d.month in (3, 4)]
    return hits[0] if hits else None


def derived_metrics(fin: pd.DataFrame) -> dict[str, Optional[float]]:
    """Multi-year measures the scorecard doesn't need but the four-investor parameters do."""
    out: dict[str, Optional[float]] = {}
    if fin is None or fin.empty:
        return out
    ni, rev, ebit = _col(fin, "net_income"), _col(fin, "revenue"), _col(fin, "ebit")
    if rev.empty:  # lenders: interest earned is the top line
        rev = _col(fin, "interest_earned")
    if ebit.empty:  # lenders: pre-tax profit is the operating result
        ebit = _col(fin, "pretax_income")
    profit = ni if len(ni) >= 3 else _col(fin, "pretax_income")
    if len(profit) >= 3:
        out["loss_years"] = float((profit.iloc[-5:] < 0).sum())
    growth = (rev / rev.shift(1) - 1)[rev.shift(1) > 0].dropna()
    if len(growth) >= 3:
        out["revenue_growth_volatility"] = float(growth.std(ddof=0))
    base = ebit.shift(1)
    change = ((ebit - base) / base.abs())[base.abs() > 0].dropna()
    if len(change) >= 2:
        out["worst_ebit_change"] = float(change.min())
    both_growth = pd.concat([growth, change], axis=1, keys=["rev", "ebit"]).dropna()
    if len(both_growth) >= 3:
        out["growth_delivery"] = float(((both_growth["rev"] > 0) & (both_growth["ebit"] > 0)).mean())
    shares = _col(fin, "shares_outstanding")
    if len(shares) >= 2 and shares.iloc[0] > 0:
        years = (shares.index[-1] - shares.index[0]).days / 365.25
        if years >= 1:
            out["share_count_cagr"] = float((shares.iloc[-1] / shares.iloc[0]) ** (1 / years) - 1)
    ce = (_col(fin, "total_equity") + _col(fin, "total_debt").reindex(fin.index).fillna(0)).dropna()
    both = pd.concat([ebit, ce], axis=1, keys=["ebit", "ce"]).dropna()
    if len(both) >= 3:
        roce = both["ebit"] / both["ce"].where(both["ce"] > 0)
        out["excess_roce_share"] = float((roce.dropna() >= 0.15).mean())
    if len(both) >= 4:
        d_ce = both["ce"].iloc[-1] - both["ce"].iloc[-4]
        d_ebit = both["ebit"].iloc[-1] - both["ebit"].iloc[-4]
        if d_ce > 0:
            out["incremental_roce"] = float(d_ebit / d_ce)
        elif d_ebit > 0:  # profit grew while capital employed shrank: the best case, not missing data
            out["incremental_roce"] = 1.0
    roe = (ni / _col(fin, "total_equity").where(lambda e: e > 0)).dropna()
    if len(roe) >= 3:
        out["excess_roe_share"] = float((roe >= 0.15).mean())
    interest = _col(fin, "interest_expense")
    if not interest.empty and not ebit.empty and ebit.iloc[-1] > 0 and interest.index[-1] == ebit.index[-1]:
        out["interest_burden"] = float(abs(interest.iloc[-1]) / ebit.iloc[-1])
    debt, cash, ebitda = _col(fin, "total_debt"), _col(fin, "cash_and_st_investments"), _col(fin, "ebitda")
    if not (debt.empty or cash.empty or ebitda.empty) and ebitda.iloc[-1] > 0:
        out["net_debt_to_ebitda"] = float((debt.iloc[-1] - cash.iloc[-1]) / ebitda.iloc[-1])
    std = _col(fin, "short_term_debt")
    if not (std.empty or debt.empty) and std.index[-1] == debt.index[-1]:
        out["short_term_debt_share"] = float(std.iloc[-1] / debt.iloc[-1]) if debt.iloc[-1] > 0 else 0.0
    capex, dep = _col(fin, "capex").iloc[-3:], _col(fin, "depreciation").iloc[-3:]
    if len(capex) >= 2 and len(dep) >= 2 and dep.sum() > 0:
        out["capex_to_depreciation"] = float(capex.abs().sum() / dep.sum())
    mat = _col(fin, "raw_material_cost")
    gm = (1 - mat / rev).dropna()
    gm = gm[rev.reindex(gm.index) > 0]
    if len(gm) >= 1 and mat.iloc[-1] > 0:  # zero material cost = services company, gross margin meaningless
        out["gross_margin"] = float(gm.iloc[-1])
        out["material_cost_share"] = float(1 - gm.iloc[-3:].mean())
        if len(gm) >= 3:
            out["gross_margin_volatility"] = float(gm.std(ddof=0))
    margin = (ebit / rev).dropna()
    if (mat.empty or mat.iloc[-1] == 0) and len(margin) >= 3:  # services: no material cost line
        out["services_ebit_margin"] = float(margin.iloc[-3:].mean())
        out["services_margin_volatility"] = float(margin.std(ddof=0))
        emp = (_col(fin, "employee_cost") / rev).dropna()
        if len(emp) >= 1:
            out["employee_cost_share"] = float(emp.iloc[-3:].mean())
    fy22, fy23 = _fy(margin.index, 2022), _fy(margin.index, 2023)
    if fy22 is not None and fy23 is not None:
        out["inflation_margin_change"] = float(margin[fy23] - margin[fy22])
    fy20, fy21 = _fy(rev.index, 2020), _fy(rev.index, 2021)
    if fy20 is not None and fy21 is not None and rev[fy20] > 0:
        out["covid_revenue_change"] = float(rev[fy21] / rev[fy20] - 1)
    roa = (ni / _col(fin, "total_assets")).dropna()
    if len(roa) >= 3:
        out["roa_volatility"] = float(roa.std(ddof=0))
    return out


def dcf_estimates(fin: Optional[pd.DataFrame], km: KeyMetrics, price: Optional[float]) -> tuple[dict, str]:
    """Base and bear two-stage FCF DCF from filed data. Returns (values, explanation)."""
    if fin is None or fin.empty or not price:
        return {}, ""
    fcf = (_col(fin, "operating_cash_flow") + _col(fin, "capex")).dropna().iloc[-3:]
    shares = _col(fin, "shares_outstanding")
    debt, cash = _col(fin, "total_debt"), _col(fin, "cash_and_st_investments")
    if len(fcf) < 2 or shares.empty or debt.empty or cash.empty:
        return {}, ""
    fcf0, net_debt = float(fcf.mean()), float(debt.iloc[-1] - cash.iloc[-1])
    if fcf0 <= 0:
        return ({"dcf_upside": -1.0, "bear_dcf_upside": -1.0},
                f"Normalised free cash flow (3y average) is negative ({fcf0 / 1e7:,.0f} Cr): the cash flows "
                "on record do not support any equity value.")
    g = min(max(km.values.get("revenue_cagr") or 0.05, 0.0), 0.15)
    base = dcf_value(fcf0, g, 5, DCF_TERMINAL_GROWTH, DCF_DISCOUNT_RATE, net_debt, float(shares.iloc[-1]))
    bear = dcf_value(fcf0, min(g / 2, 0.05), 5, DCF_TERMINAL_GROWTH - 0.01, DCF_DISCOUNT_RATE + 0.01,
                     net_debt, float(shares.iloc[-1]))
    why = (f"FCF₀ ₹{fcf0 / 1e7:,.0f} Cr (3y average), net debt ₹{net_debt / 1e7:,.0f} Cr, "
           f"{shares.iloc[-1] / 1e7:,.1f} Cr shares. Base: {g:.1%} growth for 5 years, then "
           f"{DCF_TERMINAL_GROWTH:.0%}, discounted at {DCF_DISCOUNT_RATE:.0%} → ₹{base.per_share:,.2f}/share. "
           f"Bear: {min(g / 2, 0.05):.1%} growth, {DCF_TERMINAL_GROWTH - 0.01:.0%} terminal, "
           f"{DCF_DISCOUNT_RATE + 0.01:.0%} discount → ₹{bear.per_share:,.2f}/share. Price ₹{price:,.2f}.")
    return {"dcf_upside": base.per_share / price - 1, "bear_dcf_upside": bear.per_share / price - 1,
            "dcf_base_value": base.per_share, "dcf_bear_value": bear.per_share}, why


def _rating_from(rule: Rule, value: float, note: str) -> Rating:
    cuts = " / ".join(_fmt(c, rule.unit) for c in rule.cuts)
    return Rating(param_id=rule.param_id, rating=rule.rate(value), observed=_fmt(value, rule.unit),
                  evidence=" ".join(x for x in [rule.note, note, f"Cut points for ratings 1–4: {cuts}."] if x),
                  confidence="High", source="app")


def app_ratings(km: KeyMetrics, company_type: str, fin: Optional[pd.DataFrame] = None,
                price: Optional[float] = None, evidence: Optional["Evidence"] = None) -> dict[str, Rating]:
    """Ratings the app can justify from data: filed statements (`fin` = facts_to_frame), the price,
    and public evidence collected by `eii.four_investor.evidence.collect`."""
    ev_values = dict(evidence.values) if evidence else {}
    ev_notes = dict(evidence.notes) if evidence else {}
    rpt = [ev_values[k] for k in ("rpt_purchases_pct", "rpt_sales_pct", "rpt_loans_pct", "rpt_investments_pct")
           if k in ev_values]
    if rpt:
        ev_values["rpt_max_share"] = max(rpt)
        ev_notes["rpt_max_share"] = ev_notes.get("rpt_purchases_pct", "")
    lender = company_type in FIN or company_type == "insurer"
    dcf, dcf_note = ({}, "") if lender else dcf_estimates(fin, km, price)
    v = {**km.values, **derived_metrics(fin), **dcf, **ev_values}
    rev, fcf = v.get("latest_revenue"), v.get("latest_fcf")
    v["fcf_margin"] = fcf / rev if fcf is not None and rev and rev > 0 else None
    if v.get("revenue_cagr") is not None:
        v["growth_vs_gdp"] = v["revenue_cagr"] - NOMINAL_GDP_GROWTH
    notes = {**km.notes, **ev_notes, "dcf_upside": dcf_note, "bear_dcf_upside": dcf_note}

    out: dict[str, Rating] = {}
    for rule in RULES:
        if rule.param_id in out or company_type not in rule.applies_to:
            continue
        value = v.get(rule.metric)
        if value is not None:
            out[rule.param_id] = _rating_from(rule, value, notes.get(rule.metric, ""))
    for pid in MODEL_PARAMS:
        if pid in out:
            out[pid].source, out[pid].confidence = "model", "Medium"
            out[pid].evidence += (" Mechanical DCF on basic shares and reported cash flows: convertible instruments "
                                  "and contingent liabilities are not modelled.")
    if lender:
        for pid in LENDER_NOT_APPLICABLE:
            if pid not in out:
                out[pid] = _na(pid, "Not meaningful for a lender's balance sheet; a sector substitute is used "
                                    "where one exists.")

    def put(pid: str, rating: int, observed: str, why: str, confidence: Confidence = "High") -> None:
        out[pid] = Rating(param_id=pid, rating=rating, observed=observed, evidence=why, confidence=confidence)

    # Auditor: filed opinion plus resignations announced to NSE.
    clean, resigned = v.get("audit_opinion_clean"), v.get("auditor_resignations")
    if clean is not None:
        rating = (4 if resigned == 0 else 3 if resigned is None else 2) if clean else (0 if resigned else 1)
        put("munger.integrity.auditor", rating,
            ("Unmodified opinion" if clean else "Modified opinion") +
            ("" if resigned is None else f"; {resigned:.0f} auditor resignation(s) in 3 years"),
            "Audit opinion as filed with NSE" + ("; auditor resignations not checked (announcements "
                                                 "unavailable), so capped at 3." if resigned is None else
                                                 "; resignations from NSE announcements."))
    # Promoter track record: red-flag announcements, regulatory actions and holding trend.
    if "red_flag_events" in v:
        flags, actions = v["red_flag_events"], v.get("regulatory_actions", 0)
        change = v.get("promoter_holding_change")
        rating = 0 if flags >= 2 else 1 if flags == 1 else 3
        if rating == 3 and change is not None and change < -10:
            rating = 2
        if rating >= 2 and actions >= 4:
            rating -= 1
        put("munger.integrity.track_record", rating,
            f"{flags:.0f} fraud/default/insolvency filing(s), {actions:.0f} regulatory action notice(s)"
            + ("" if change is None else f", promoter holding {change:+.1f} pp over 3 years"),
            "From 3 years of NSE announcements and shareholding patterns. Capped at 3: a clean record is "
            "necessary, not sufficient, evidence of good management.", "Medium")
    # Acquisitions: only the absence of deals can be judged from filings.
    if v.get("acquisitions_3y") == 0:
        put("munger.capital.acquisitions", 3, "No acquisitions announced in 3 years",
            "No acquisition announcements to NSE in 3 years, so no evidence of value-destroying M&A. Rated 3, "
            "not 4: there is no record to judge discipline by.", "Medium")
    # Dividends and buybacks against cash generation and returns.
    divs, ni = (_col(fin, "dividends_paid").abs().iloc[-3:], _col(fin, "net_income").iloc[-3:]) \
        if fin is not None and not fin.empty else (pd.Series(dtype=float), pd.Series(dtype=float))
    if len(ni) >= 2 and ni.sum() > 0:
        payout = float(divs.sum() / ni.sum()) if not divs.empty else 0.0
        roe, buybacks = v.get("roe_avg_3y"), v.get("buybacks_3y", 0)
        if payout > 1.0:
            rating = 1
        elif payout >= 0.15 or buybacks:
            rating = 4 if lender or (v.get("fcf_margin") or 0) > 0 else 2
        else:
            rating = 3 if roe is not None and roe >= 0.15 else 1
        put("munger.capital.dividends_buybacks", rating,
            f"Payout {payout:.0%} of 3y profit" + (f", {buybacks:.0f} buyback(s)" if buybacks else ""),
            "Rewards distributions covered by free cash flow, and retention only when returns justify it "
            "(ROE ≥ 15%). Payout above 100% of profit is rated poor.")
    # Survival under stress: the weaker of interest cover and liquidity.
    ic, cr = v.get("interest_coverage"), v.get("current_ratio")
    if ic is not None and cr is not None and not lender:
        ric = Rule("", "", (1.5, 3, 6, 12), "x").rate(ic)
        rcr = Rule("", "", (0.8, 1.0, 1.3, 1.8), "x").rate(cr)
        put("munger.protection.survival", min(ric, rcr), f"Interest cover {ic:.1f}x, current ratio {cr:.2f}",
            "The weaker of interest coverage and liquidity: survival needs both.")
    if "munger.integrity.pledging" not in out and v.get("promoter_holding") == 0:
        out["munger.integrity.pledging"] = _na("munger.integrity.pledging", "No promoter group (professionally "
                                                                            "managed, widely held company).")
    if v.get("short_term_debt_share") is None and v.get("debt_to_equity") == 0 and not lender:
        put("dalio.credit.refinancing", 4, "No borrowings", "Debt-free: nothing to refinance.")
    return out


def sector_ratings(sector: Optional[str], company_type: str, evidence: Optional["Evidence"] = None,
                   fin: Optional[pd.DataFrame] = None) -> tuple[Optional[str], dict[str, Rating]]:
    """Industry-level ratings and applicability from the sector profile. Returns (profile name, ratings)."""
    name = resolve_sector(sector)
    profile: Optional[SectorProfile] = PROFILES.get(name) if name else None
    if profile is None:
        return None, {}
    why = f"Sector profile: {name}. {profile.rationale}"
    out = {pid: Rating(param_id=pid, rating=r, observed=f"{name} sector", evidence=why, confidence="Low",
                       source="sector") for pid, r in zip(PROFILE_PARAMS, profile.ratings)}
    ev = evidence.values if evidence else {}
    if not profile.innovation:
        for pid in INNOVATION_PARAMS:
            out[pid] = _na(pid, f"R&D, patents and new products do not drive competitiveness in {name}.", "sector")
    else:
        if name not in PATENT_SECTORS:
            out["fisher.innovation.patents"] = _na(
                "fisher.innovation.patents", f"Patents are not a primary competitive driver in {name}; technology "
                                             "and product strength are assessed under the other innovation "
                                             "parameters.", "sector")
    if profile.innovation and "rnd_pct" in ev:
        rule = Rule("fisher.innovation.rnd", "rnd_pct", (0.001, 0.005, 0.01, 0.03), "pct", ALL,
                    "R&D spend as % of turnover (BRSR).")
        out["fisher.innovation.rnd"] = _rating_from(rule, ev["rnd_pct"], evidence.notes.get("rnd_pct", ""))
    if not profile.brand:
        out["buffett.moat.brand"] = _na("buffett.moat.brand", f"Consumer brand is not a competitive factor in "
                                                              f"{name}.", "sector")
    else:
        derived = derived_metrics(fin)
        if derived.get("gross_margin") is not None:
            rule = Rule("buffett.moat.brand", "gross_margin", (0.25, 0.35, 0.45, 0.55), "pct", ALL,
                        f"Proxy in a brand-led sector ({name}): gross margin, latest year — brands command a price "
                        "premium over input cost. A research run replaces this with brand evidence.")
            value = derived["gross_margin"]
        elif derived.get("services_ebit_margin") is not None:
            rule = Rule("buffett.moat.brand", "services_ebit_margin", (0.08, 0.12, 0.18, 0.25), "pct", ALL,
                        f"Proxy in a brand-led services sector ({name}): EBIT margin, 3y average — a trusted brand "
                        "wins work at better prices. A research run replaces this with brand evidence.")
            value = derived["services_ebit_margin"]
        else:
            rule = None
        if rule is not None:
            out[rule.param_id] = _rating_from(rule, value, "")
            out[rule.param_id].confidence, out[rule.param_id].source = "Medium", "sector"
    if not profile.distribution:
        out["fisher.competition.distribution"] = _na(
            "fisher.competition.distribution", f"Sales in {name} are contract/tender-based, not through a "
                                               "dealer network.", "sector")
    elif ev.get("dealers_count"):
        rule = Rule("fisher.competition.distribution", "dealers_count", (20, 100, 500, 2000), "num", ALL,
                    "Dealers/distributors the company sells through (BRSR).")
        out[rule.param_id] = _rating_from(rule, ev["dealers_count"], evidence.notes.get("dealers_count", ""))
    return name, out


# ---------------------------------------------------------------------------- scoring

@dataclass
class CategoryScore:
    category: str
    points: float
    rated_weight: int
    applicable_weight: int


@dataclass
class InvestorScore:
    investor: str
    score: Optional[float]  # 0–100 over the rated weight
    coverage: float  # rated weight / applicable weight
    categories: list[CategoryScore]
    applicable_weight: int = 100

    @property
    def provisional(self) -> bool:
        return self.coverage < MIN_COVERAGE


def merge(app: dict[str, Rating], claude: dict[str, Rating],
          sector: Optional[dict[str, Rating]] = None) -> dict[str, Rating]:
    """Precedence: app data > Claude research > app DCF model and sector profile. Claude's unrated entries
    are ignored."""
    firm = {k: r for k, r in app.items() if r.source != "model"}
    model = {k: r for k, r in app.items() if r.source == "model"}
    return {**(sector or {}), **model, **{k: r for k, r in claude.items() if r.rating is not None}, **firm}


def score_investor(investor: str, ratings: dict[str, Rating]) -> InvestorScore:
    cats: dict[str, CategoryScore] = {}
    for p in PARAMS:
        if p.investor != investor:
            continue
        c = cats.setdefault(p.category, CategoryScore(p.category, 0.0, 0, 0))
        r = ratings.get(p.id)
        if r is not None and not r.applicable:
            continue
        c.applicable_weight += p.weight
        if r is not None and r.rating is not None:
            c.points += r.rating / 4 * p.weight
            c.rated_weight += p.weight
    rated = sum(c.rated_weight for c in cats.values())
    applicable = sum(c.applicable_weight for c in cats.values())
    earned = sum(c.points for c in cats.values())
    return InvestorScore(investor, round(100 * earned / rated, 1) if rated else None,
                         rated / applicable if applicable else 0.0, list(cats.values()), applicable)


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

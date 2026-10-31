"""Financial-ratio calculations.

Pure functions over pandas objects. Rules:
* A ratio is `None`/NaN whenever an input is missing, or the denominator is zero
  or economically meaningless (e.g. negative equity for ROE). Nothing is imputed.
* Ratios use end-of-period balances (documented in Settings_Methodology).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable, Optional

import numpy as np
import pandas as pd

from eii.models import AuditInfo, FinancialFact, GovernanceInputs, MarketSnapshot, OwnershipSnapshot

METRIC_LABELS = {
    "revenue": "Revenue", "ebitda": "EBITDA", "ebit": "EBIT", "pretax_income": "Profit before tax",
    "tax_provision": "Tax", "net_income": "Net profit", "diluted_eps": "Diluted EPS",
    "interest_expense": "Interest expense", "total_assets": "Total assets",
    "total_equity": "Shareholders' equity", "total_debt": "Total debt",
    "cash_and_st_investments": "Cash & short-term investments", "current_assets": "Current assets",
    "current_liabilities": "Current liabilities", "receivables": "Receivables",
    "inventory": "Inventory", "shares_outstanding": "Shares outstanding",
    "operating_cash_flow": "Operating cash flow", "capex": "Capital expenditure",
    "free_cash_flow": "Free cash flow", "dividends_paid": "Dividends paid",
    "other_income": "Other income", "depreciation": "Depreciation & amortisation",
    "short_term_debt": "Short-term borrowings", "raw_material_cost": "Material & traded-goods cost",
    "employee_cost": "Employee benefit expense",
    # banks / NBFCs
    "interest_earned": "Interest earned", "interest_expended": "Interest expended / finance costs",
    "net_interest_income": "Net interest income", "operating_expenses": "Operating expenses",
    "pre_provision_profit": "Pre-provision operating profit", "provisions": "Provisions / impairment",
    "gross_npa": "Gross NPA", "net_npa": "Net NPA", "deposits": "Deposits", "advances": "Advances / loans",
    "gnpa_pct": "Gross NPA ratio", "nnpa_pct": "Net NPA ratio", "roa_reported": "Return on assets (reported)",
    "cet1_ratio": "CET1 ratio", "diluted_eps_reported": "Diluted EPS as filed (before split/bonus restatement)",
}
RATIO_METRICS = {"gnpa_pct", "nnpa_pct", "roa_reported", "cet1_ratio"}  # stored as decimals
LENDER_TYPES = {"bank", "nbfc", "insurer"}

RATIO_LABELS = {
    "revenue_growth": "Revenue growth (YoY)", "net_income_growth": "Net profit growth (YoY)",
    "ebitda_margin": "EBITDA margin", "ebit_margin": "EBIT margin", "net_margin": "Net margin",
    "roe": "Return on equity", "roce": "Return on capital employed",
    "debt_to_equity": "Debt / equity", "interest_coverage": "Interest coverage (x)",
    "current_ratio": "Current ratio (x)", "fcf": "Free cash flow", "fcf_margin": "FCF margin",
    "cash_conversion": "Cash conversion (OCF / net profit)", "accruals_ratio": "Accruals ratio",
    "effective_tax_rate": "Effective tax rate",
}


def _clean(x) -> Optional[float]:
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def safe_div(numerator, denominator) -> Optional[float]:
    n, d = _clean(numerator), _clean(denominator)
    if n is None or d is None or d == 0:
        return None
    return n / d


def cagr(start_value, end_value, years: float) -> Optional[float]:
    """Compound annual growth rate. None if either end is non-positive or years <= 0."""
    s, e = _clean(start_value), _clean(end_value)
    if s is None or e is None or years is None or years <= 0 or s <= 0 or e <= 0:
        return None
    return (e / s) ** (1.0 / years) - 1.0


def facts_to_frame(facts: Iterable[FinancialFact]) -> pd.DataFrame:
    """Pivot facts to a frame: index = fiscal period end (ascending), columns = metrics."""
    rows = [{"period_end": f.period_end, "metric": f.metric, "value": f.value} for f in facts]
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).pivot_table(index="period_end", columns="metric", values="value",
                                        aggfunc="last")
    df.index = pd.to_datetime(df.index)
    df.columns.name = None
    return df.sort_index().astype(float)


def _col(fin: pd.DataFrame, name: str) -> pd.Series:
    if name in fin.columns:
        return fin[name].astype(float)
    return pd.Series(np.nan, index=fin.index, dtype=float)


def _ratio(num: pd.Series, den: pd.Series, positive_den: bool = False) -> pd.Series:
    den = den.where(den > 0) if positive_den else den.where(den != 0)
    return (num / den).replace([np.inf, -np.inf], np.nan)


def _growth(s: pd.Series) -> pd.Series:
    prev = s.shift(1)
    return ((s - prev) / prev.where(prev > 0)).replace([np.inf, -np.inf], np.nan)


def compute_ratios(fin: pd.DataFrame) -> pd.DataFrame:
    """Per-period ratios. Input from `facts_to_frame`."""
    if fin.empty:
        return pd.DataFrame()
    rev, ni, ebit = _col(fin, "revenue"), _col(fin, "net_income"), _col(fin, "ebit")
    equity, assets = _col(fin, "total_equity"), _col(fin, "total_assets")
    ocf, capex = _col(fin, "operating_cash_flow"), _col(fin, "capex")
    reported_fcf = _col(fin, "free_cash_flow")
    fcf = reported_fcf.where(reported_fcf.notna(), ocf - capex.abs())
    out = pd.DataFrame(index=fin.index)
    out["revenue_growth"] = _growth(rev)
    out["net_income_growth"] = _growth(ni)
    out["ebitda_margin"] = _ratio(_col(fin, "ebitda"), rev, positive_den=True)
    out["ebit_margin"] = _ratio(ebit, rev, positive_den=True)
    out["net_margin"] = _ratio(ni, rev, positive_den=True)
    out["roe"] = _ratio(ni, equity, positive_den=True)
    out["roce"] = _ratio(ebit, assets - _col(fin, "current_liabilities"), positive_den=True)
    out["debt_to_equity"] = _ratio(_col(fin, "total_debt"), equity, positive_den=True)
    out["interest_coverage"] = _ratio(ebit, _col(fin, "interest_expense").abs(), positive_den=True)
    out["current_ratio"] = _ratio(_col(fin, "current_assets"), _col(fin, "current_liabilities"),
                                  positive_den=True)
    out["fcf"] = fcf
    out["fcf_margin"] = _ratio(fcf, rev, positive_den=True)
    out["cash_conversion"] = _ratio(ocf, ni, positive_den=True)
    out["accruals_ratio"] = _ratio(ni - ocf, assets, positive_den=True)
    out["effective_tax_rate"] = _ratio(_col(fin, "tax_provision"), _col(fin, "pretax_income"),
                                       positive_den=True)
    return out


def period_cagr(series: pd.Series, max_years: int = 3, min_years: int = 2
                ) -> tuple[Optional[float], Optional[str]]:
    """CAGR over the longest available span up to `max_years`, using actual dates."""
    s = series.dropna()
    if len(s) < 2:
        return None, None
    end_date, end_val = s.index[-1], s.iloc[-1]
    candidates = s[s.index <= end_date - pd.DateOffset(years=min_years) + pd.Timedelta(days=31)]
    candidates = candidates[candidates.index >= end_date - pd.DateOffset(years=max_years) - pd.Timedelta(days=31)]
    if candidates.empty:
        return None, None
    start_date, start_val = candidates.index[0], candidates.iloc[0]
    years = round((end_date - start_date).days / 365.25)
    value = cagr(start_val, end_val, years)
    note = f"{years}y CAGR, FY ending {start_date:%b %Y} → {end_date:%b %Y}"
    return value, note


def _avg_last(s: pd.Series, n: int = 3, min_n: int = 2) -> Optional[float]:
    s = s.dropna().iloc[-n:]
    return float(s.mean()) if len(s) >= min_n else None


def _latest(s: pd.Series) -> Optional[float]:
    s = s.dropna()
    return float(s.iloc[-1]) if not s.empty else None


def momentum(price_history: Optional[pd.DataFrame], as_of: Optional[datetime] = None) -> dict:
    """Trailing price statistics using only prices on or before `as_of` (no look-ahead)."""
    out = dict(return_6m=None, return_12m=None, price_vs_200dma=None, high_52w=None, low_52w=None)
    if price_history is None or price_history.empty or "close" not in price_history:
        return out
    px = price_history["close"].dropna().sort_index()
    if as_of is not None:
        cutoff = pd.Timestamp(as_of)
        if cutoff.tzinfo is not None:
            cutoff = cutoff.tz_localize(None)
        px = px[px.index <= cutoff]
    if px.empty:
        return out
    last_date, last = px.index[-1], float(px.iloc[-1])

    def back(months: int) -> Optional[float]:
        past = px[px.index <= last_date - pd.DateOffset(months=months)]
        # require the observation to be within 10 days of the target date
        if past.empty or (last_date - pd.DateOffset(months=months) - past.index[-1]).days > 10:
            return None
        return float(past.iloc[-1])

    p6, p12 = back(6), back(12)
    out["return_6m"] = safe_div(last - p6, p6) if p6 else None
    out["return_12m"] = safe_div(last - p12, p12) if p12 else None
    if len(px) >= 200:
        out["price_vs_200dma"] = last / float(px.iloc[-200:].mean()) - 1
    window = px[px.index > last_date - pd.DateOffset(years=1)]
    out["high_52w"], out["low_52w"] = float(window.max()), float(window.min())
    return out


def financial_currency(facts: Iterable[FinancialFact]) -> Optional[str]:
    """Currency of the monetary statement figures (from revenue, else any monetary fact)."""
    units = [f.unit for f in facts if f.metric == "revenue"] or \
            [f.unit for f in facts if f.unit and "/" not in f.unit and f.unit != "shares"]
    return units[0] if units else None


@dataclass
class KeyMetrics:
    values: dict[str, Optional[float]] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)
    latest_period: Optional[date] = None
    years_of_data: int = 0
    currency_mismatch: Optional[str] = None  # message when statements and price differ in currency


def key_metrics(fin: pd.DataFrame, market: Optional[MarketSnapshot] = None,
                governance: Optional[GovernanceInputs] = None,
                fin_currency: Optional[str] = "INR",
                ownership: Optional[OwnershipSnapshot] = None,
                audit: Optional[AuditInfo] = None,
                company_type: str = "unknown") -> KeyMetrics:
    """Summary metrics consumed by the scorecard and valuation views.

    Price-based multiples are only computed when the statements and the share
    price are in the same currency; no FX conversion is attempted.
    """
    km = KeyMetrics()
    price_ccy = (market.currency or "INR") if market else None
    if market and fin_currency and price_ccy and fin_currency.upper() != price_ccy.upper():
        km.currency_mismatch = (f"Financial statements are reported in {fin_currency} but the share price is in "
                                f"{price_ccy}. Price-based multiples, scenarios and DCF are disabled because "
                                "currency conversion is not implemented.")
    v = km.values
    ratios = compute_ratios(fin) if not fin.empty else pd.DataFrame()
    if not fin.empty:
        rev = _col(fin, "revenue")
        km.years_of_data = int(rev.notna().sum())
        km.latest_period = rev.dropna().index[-1].date() if rev.notna().any() else None
        v["roe_avg_3y"] = _avg_last(ratios["roe"])
        v["roce_avg_3y"] = _avg_last(ratios["roce"])
        v["ebit_margin_avg_3y"] = _avg_last(ratios["ebit_margin"])
        em = ratios["ebit_margin"].dropna()
        v["ebit_margin_volatility"] = float(em.std(ddof=0)) if len(em) >= 3 else None
        v["revenue_cagr"], note = period_cagr(rev)
        if note:
            km.notes["revenue_cagr"] = note
        eps = _col(fin, "diluted_eps")
        v["eps_cagr"], note = period_cagr(eps)
        if note:
            km.notes["eps_cagr"] = note
        if v["eps_cagr"] is None and eps.notna().sum() >= 2:
            km.notes["eps_cagr"] = "Not computed: EPS was zero or negative at the start or end of the period."
        v["debt_to_equity"] = _latest(ratios["debt_to_equity"])
        v["interest_coverage"] = _latest(ratios["interest_coverage"])
        if v["interest_coverage"] is None and _latest(_col(fin, "total_debt")) == 0:
            km.notes["interest_coverage"] = "Company reports zero debt; coverage not meaningful."
        v["current_ratio"] = _latest(ratios["current_ratio"])
        ocf3, ni3 = _col(fin, "operating_cash_flow").iloc[-3:], _col(fin, "net_income").iloc[-3:]
        both = ocf3.notna() & ni3.notna()
        v["cash_conversion_3y"] = (safe_div(ocf3[both].sum(), ni3[both].sum())
                                   if both.sum() >= 2 and ni3[both].sum() > 0 else None)
        v["accruals_ratio"] = _latest(ratios["accruals_ratio"])
        v["latest_revenue"] = _latest(rev)
        v["latest_net_income"] = _latest(_col(fin, "net_income"))
        v["latest_eps"] = _latest(eps)
        v["latest_fcf"] = _latest(ratios["fcf"])
        _financial_sector_metrics(fin, km)

    if market and market.price:
        mcap = market.market_cap
        if mcap is None and market.shares_outstanding:
            mcap = market.price * market.shares_outstanding
            km.notes["market_cap"] = "Derived as price × shares outstanding."
        elif mcap is None and (filed_shares := _latest(_col(fin, "shares_outstanding"))):
            mcap = market.price * filed_shares
            km.notes["market_cap"] = "Derived as price × latest filed shares outstanding."
        v["market_cap"] = mcap
        eps_latest = v.get("latest_eps")
        if km.currency_mismatch:
            for k in ("pe", "pb", "ev_ebitda", "fcf_yield", "dividend_yield"):
                km.notes[k] = "Not computed: statement and price currencies differ."
        elif eps_latest is not None and eps_latest > 0:
            v["pe"] = market.price / eps_latest
        elif eps_latest is not None:
            km.notes["pe"] = "Not meaningful: latest EPS is zero or negative."
        if mcap and not fin.empty and not km.currency_mismatch:
            equity = _latest(_col(fin, "total_equity"))
            v["pb"] = mcap / equity if equity and equity > 0 else None
            debt = _latest(_col(fin, "total_debt"))
            cash = _latest(_col(fin, "cash_and_st_investments"))
            ebitda = _latest(_col(fin, "ebitda"))
            if debt is not None and cash is not None:
                ev = mcap + debt - cash
                v["enterprise_value"] = ev
                v["ev_ebitda"] = ev / ebitda if ebitda and ebitda > 0 else None
            fcf = v.get("latest_fcf")
            v["fcf_yield"] = fcf / mcap if fcf is not None else None
            if company_type in LENDER_TYPES:
                # Debt and cash flows are a lender's raw material: EV and FCF are not meaningful.
                for k in ("enterprise_value", "ev_ebitda", "fcf_yield"):
                    v[k] = None
                    km.notes[k] = "Not meaningful for banks/NBFCs/insurers."
            div = _latest(_col(fin, "dividends_paid"))
            v["dividend_yield"] = abs(div) / mcap if div is not None else None
        v["return_6m"] = market.return_6m
        v["return_12m"] = market.return_12m
        v["price_vs_200dma"] = market.price_vs_200dma

    if governance:
        v["governance_rating"] = governance.governance_rating
        v["promoter_pledge_pct"] = governance.promoter_pledge_pct
        v["macro_resilience_rating"] = governance.macro_resilience_rating
    # Filed data takes precedence over analyst-entered values for the same fact.
    if ownership and ownership.promoter_pledged_pct_of_promoter is not None:
        v["promoter_pledge_pct"] = ownership.promoter_pledged_pct_of_promoter
        km.notes["promoter_pledge_pct"] = f"Reported to NSE, as of {ownership.as_of:%d %b %Y}."
    elif v.get("promoter_pledge_pct") is not None:
        km.notes["promoter_pledge_pct"] = "Analyst-entered."
    if audit and audit.opinion_unmodified is not None:
        v["audit_opinion_clean"] = 1.0 if audit.opinion_unmodified else 0.0
        km.notes["audit_opinion_clean"] = f"FY{audit.period_end.year} filing: {audit.opinion}"
    return km


def _financial_sector_metrics(fin: pd.DataFrame, km: KeyMetrics) -> None:
    """Bank/NBFC metrics. Computed only from columns that exist; never approximated."""
    v = km.values
    ni, assets, equity = _col(fin, "net_income"), _col(fin, "total_assets"), _col(fin, "total_equity")
    v["roa_avg_3y"] = _avg_last(_ratio(ni, assets, positive_den=True))
    if "net_interest_income" not in fin.columns:
        return
    nii = _col(fin, "net_interest_income")
    v["nii_to_assets"] = _latest(_ratio(nii, assets, positive_den=True))
    km.notes["nii_to_assets"] = "NII ÷ year-end total assets (a proxy for net interest margin)."
    opex = _col(fin, "operating_expenses")
    v["cost_to_income"] = _latest(_ratio(opex, nii + _col(fin, "other_income"), positive_den=True))
    v["credit_cost"] = _latest(_ratio(_col(fin, "provisions"), _col(fin, "advances"), positive_den=True))
    v["leverage"] = _latest(_ratio(assets, equity, positive_den=True))
    for metric in ("gnpa_pct", "nnpa_pct", "cet1_ratio"):
        v[metric] = _latest(_col(fin, metric))
    for metric, series in (("nii_cagr", nii), ("advances_cagr", _col(fin, "advances")),
                           ("deposits_cagr", _col(fin, "deposits"))):
        v[metric], note = period_cagr(series)
        if note:
            km.notes[metric] = note

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from eii.calculations import (cagr, compute_ratios, facts_to_frame, key_metrics, momentum,
                              period_cagr, safe_div)
from eii.models import DataStatus, FinancialFact, GovernanceInputs


def test_safe_div_handles_missing_and_zero():
    assert safe_div(10, 4) == 2.5
    assert safe_div(10, 0) is None
    assert safe_div(None, 4) is None
    assert safe_div(10, float("nan")) is None


def test_cagr_basic_and_invalid_inputs():
    assert cagr(100, 133.1, 3) == pytest.approx(0.10)
    assert cagr(-100, 120, 3) is None  # negative base is not a growth rate
    assert cagr(100, -5, 3) is None
    assert cagr(100, 120, 0) is None
    assert cagr(None, 120, 2) is None


def test_facts_to_frame_sorted_and_pivoted(facts):
    df = facts_to_frame(facts)
    assert list(df.index.year) == [2022, 2023, 2024, 2025]
    assert df.loc["2025-03-31", "revenue"] == 1331
    assert facts_to_frame([]).empty


def test_ratios_match_hand_calculation(facts):
    r = compute_ratios(facts_to_frame(facts)).loc["2025-03-31"]
    assert r["revenue_growth"] == pytest.approx(0.10)
    assert r["ebit_margin"] == pytest.approx(266.2 / 1331)
    assert r["net_margin"] == pytest.approx(0.12)
    assert r["roe"] == pytest.approx(159.72 / 1050)
    assert r["roce"] == pytest.approx(266.2 / (1800 - 440))
    assert r["debt_to_equity"] == pytest.approx(250 / 1050)
    assert r["interest_coverage"] == pytest.approx(266.2 / 22)
    assert r["current_ratio"] == pytest.approx(760 / 440)
    assert r["fcf"] == pytest.approx(190 - 70)  # derived: OCF − |capex|
    assert r["cash_conversion"] == pytest.approx(190 / 159.72)
    assert r["accruals_ratio"] == pytest.approx((159.72 - 190) / 1800)


def test_ratios_do_not_invent_values_when_inputs_missing():
    facts = [FinancialFact(metric="revenue", period_end=date(2024, 3, 31), value=100),
             FinancialFact(metric="net_income", period_end=date(2024, 3, 31), value=10),
             FinancialFact(metric="total_equity", period_end=date(2024, 3, 31), value=-50)]
    r = compute_ratios(facts_to_frame(facts)).iloc[-1]
    assert r["net_margin"] == pytest.approx(0.10)
    assert np.isnan(r["roe"])            # negative equity -> not meaningful
    assert np.isnan(r["ebit_margin"])    # EBIT missing
    assert np.isnan(r["debt_to_equity"])
    assert np.isnan(r["revenue_growth"])  # only one year


def test_period_cagr_uses_actual_span(facts):
    df = facts_to_frame(facts)
    value, note = period_cagr(df["revenue"])
    assert value == pytest.approx(0.10)
    assert note.startswith("3y CAGR")
    short, _ = period_cagr(df["revenue"].iloc[-2:])  # one-year span < min_years
    assert short is None


def test_key_metrics_valuation_and_governance(facts, market):
    km = key_metrics(facts_to_frame(facts), market, GovernanceInputs(governance_rating=4))
    v = km.values
    assert km.years_of_data == 4
    assert v["pe"] == pytest.approx(319.44 / 15.972)
    assert v["pb"] == pytest.approx(3194.4 / 1050)
    assert v["enterprise_value"] == pytest.approx(3194.4 + 250 - 180)
    assert v["ev_ebitda"] == pytest.approx((3194.4 + 250 - 180) / 330)
    assert v["fcf_yield"] == pytest.approx(120 / 3194.4)
    assert v["dividend_yield"] == pytest.approx(40 / 3194.4)
    assert v["eps_cagr"] == pytest.approx(0.10)
    assert v["cash_conversion_3y"] == pytest.approx((160 + 170 + 190) / (132 + 145.2 + 159.72))
    assert v["governance_rating"] == 4
    assert v["promoter_pledge_pct"] is None  # not assessed stays None


def test_key_metrics_negative_eps_gives_no_pe(market):
    facts = [FinancialFact(metric="diluted_eps", period_end=date(2025, 3, 31), value=-2.0),
             FinancialFact(metric="revenue", period_end=date(2025, 3, 31), value=100)]
    km = key_metrics(facts_to_frame(facts), market)
    assert "pe" not in km.values
    assert "negative" in km.notes["pe"]


def _prices(days=400, start=100.0, daily=0.001):
    idx = pd.bdate_range("2024-01-01", periods=days)
    return pd.DataFrame({"close": start * (1 + daily) ** np.arange(days)}, index=idx)


def test_momentum_returns():
    px = _prices()
    m = momentum(px)
    last_date = px.index[-1]
    p12 = px["close"][px.index <= last_date - pd.DateOffset(months=12)].iloc[-1]
    assert m["return_12m"] == pytest.approx(px["close"].iloc[-1] / p12 - 1)
    assert m["price_vs_200dma"] > 0
    assert m["high_52w"] == pytest.approx(px["close"].iloc[-1])


def test_momentum_respects_as_of_no_lookahead():
    px = _prices()
    cutoff = px.index[300]
    m = momentum(px, as_of=cutoff)
    assert m["high_52w"] == pytest.approx(px["close"].iloc[300])  # later prices ignored


def test_momentum_insufficient_history():
    m = momentum(_prices(days=50))
    assert m["return_12m"] is None and m["price_vs_200dma"] is None
    assert momentum(None)["return_6m"] is None


def test_currency_mismatch_disables_price_multiples(facts, market):
    """Regression: Yahoo reports some Indian companies' statements in USD while the price is INR."""
    from eii.calculations.financials import financial_currency
    usd_facts = [f.model_copy(update={"unit": "USD/share" if f.metric == "diluted_eps" else "USD"}) for f in facts]
    assert financial_currency(usd_facts) == "USD"
    km = key_metrics(facts_to_frame(usd_facts), market, fin_currency="USD")
    assert km.currency_mismatch and "USD" in km.currency_mismatch
    for k in ("pe", "pb", "ev_ebitda", "fcf_yield", "dividend_yield"):
        assert km.values.get(k) is None
    assert km.values["roe_avg_3y"] is not None  # currency-neutral ratios still computed

from datetime import datetime, timezone

import pytest

from eii.calculations.financials import KeyMetrics
from eii.four_investor.claude_run import build_user_prompt
from eii.four_investor.framework import (INVESTORS, PARAMS, Rating, Rule, app_ratings, combined_score, merge,
                                         score_band, score_investor)
from eii.four_investor.research import ClaudeAssessment, ParamAssessment, ResearchRun, load_run, save_run
from eii.models import ResearchReport, SecurityIdentity


def test_framework_has_every_parameter_at_its_weight():
    for inv in INVESTORS:
        assert sum(p.weight for p in PARAMS if p.investor == inv) == 100
    assert len({p.id for p in PARAMS}) == len(PARAMS) == 77
    assert {p.label for p in PARAMS if p.investor == "buffett" and p.category == "Intrinsic value and margin of safety"} \
        == {"DCF intrinsic value", "Owner earnings valuation", "Historical valuation comparison", "Margin of safety"}


def test_rule_counts_cut_points_cleared_in_either_direction():
    up = Rule("buffett.economics.roe", "roe_avg_3y", (0.06, 0.10, 0.15, 0.20), "pct")
    assert [up.rate(x) for x in (0.02, 0.06, 0.12, 0.25)] == [0, 1, 2, 4]
    down = Rule("buffett.strength.leverage", "debt_to_equity", (2.0, 1.0, 0.5, 0.2), "x")
    assert [down.rate(x) for x in (3.0, 1.5, 0.4, 0.0)] == [0, 1, 3, 4]


def _km(**values):
    return KeyMetrics(values=values)


def test_app_ratings_use_filed_metrics_and_sector_substitutes():
    km = _km(roce_avg_3y=0.18, roa_avg_3y=0.015, roe_avg_3y=0.12, debt_to_equity=0.3, promoter_pledge_pct=0.0,
             latest_revenue=1000.0, latest_fcf=60.0, audit_opinion_clean=1.0)
    ind = app_ratings(km, "industrial")
    assert ind["buffett.economics.roce"].rating == 3 and ind["buffett.economics.roce"].observed == "18.0%"
    assert ind["buffett.strength.fcf"].rating == 3  # 6% FCF margin
    assert ind["munger.integrity.pledging"].rating == 4
    assert ind["munger.integrity.auditor"].rating == 3  # capped: resignations not checked
    bank = app_ratings(km, "bank")
    assert "ROA" in bank["buffett.economics.roce"].evidence  # lender substitute
    assert "buffett.strength.leverage" not in bank  # D/E is not a lender metric
    assert all(r.source == "app" for r in ind.values())


def test_scores_exclude_unrated_and_app_ratings_win():
    app = {"buffett.economics.roce": Rating(param_id="buffett.economics.roce", rating=4)}
    claude = {"buffett.economics.roce": Rating(param_id="buffett.economics.roce", rating=0, source="claude"),
              "buffett.moat.brand": Rating(param_id="buffett.moat.brand", rating=2, source="claude"),
              "buffett.moat.pricing_power": Rating(param_id="buffett.moat.pricing_power", rating=None, source="claude")}
    merged = merge(app, claude)
    assert merged["buffett.economics.roce"].rating == 4 and "buffett.moat.pricing_power" not in merged
    s = score_investor("buffett", merged)
    # 7 pts (ROCE 4/4) + 2.5 pts (brand 2/4 of 5) over 12 rated points
    assert s.score == pytest.approx(100 * 9.5 / 12, abs=0.1) and s.coverage == pytest.approx(0.12)
    assert s.provisional
    assert score_investor("fisher", merged).score is None
    assert combined_score([s, score_investor("fisher", merged)]) is None


def test_combined_and_bands():
    full = {p.id: Rating(param_id=p.id, rating=3) for p in PARAMS}
    scores = [score_investor(i, full) for i in INVESTORS]
    assert all(x.score == 75 and x.coverage == 1 for x in scores)
    assert combined_score(scores) == 75 and score_band(75) == "Good" and score_band(29) == "Critical"


def test_research_run_round_trip_and_claude_ratings(tmp_path):
    run = ResearchRun(identity_key="INE000A01011", company_name="Alpha", generated_at=datetime.now(timezone.utc),
                      model="m", horizon="3–5 years",
                      assessment=ClaudeAssessment(
                          assessments=[ParamAssessment(param_id="fisher.innovation.rnd", rating=1, evidence="AR p.12",
                                                       explanation="tiny R&D", confidence="Medium")],
                          report_markdown="# report"))
    save_run(tmp_path, run)
    loaded = load_run(tmp_path, "INE000A01011")
    r = loaded.claude_ratings()["fisher.innovation.rnd"]
    assert r.rating == 1 and r.source == "claude" and "AR p.12" in r.evidence
    assert load_run(tmp_path, "missing") is None


def test_prompt_lists_every_parameter_and_marks_app_ratings():
    report = ResearchReport(identity=SecurityIdentity(company_name="Alpha Industries Limited", nse_symbol="ALPHAIND"))
    km = _km(roe_avg_3y=0.2)
    fixed = app_ratings(km, "industrial")
    prompt = build_user_prompt(report, km, fixed, "3–5 years")
    assert all(f"| {p.id} |" in prompt for p in PARAMS)
    assert "| buffett.economics.roe | buffett | Business economics | ROE consistency | 5 | 4 (Excellent) — 20.0% |" in prompt


def test_derived_metrics_from_statements():
    import pandas as pd
    idx = pd.to_datetime(["2021-03-31", "2022-03-31", "2023-03-31", "2024-03-31", "2025-03-31"])
    fin = pd.DataFrame({
        "revenue": [100, 110, 121, 133.1, 146.41],  # steady 10% growth
        "net_income": [-5, 8, 10, 12, 14],
        "ebit": [10, 20, 15, 24, 30],
        "total_equity": [100, 110, 120, 130, 140], "total_debt": [50, 50, 50, 50, 50],
        "interest_expense": [5, 5, 5, 5, 6], "cash_and_st_investments": [10, 10, 10, 10, 10],
        "ebitda": [20, 30, 25, 34, 40], "capex": [-20, -20, -20, -20, -30], "depreciation": [10, 10, 10, 10, 10],
        "shares_outstanding": [100, 100, 100, 100, 110]}, index=idx)
    from eii.four_investor.framework import derived_metrics
    d = derived_metrics(fin)
    assert d["loss_years"] == 1 and d["revenue_growth_volatility"] == pytest.approx(0, abs=1e-9)
    assert d["worst_ebit_change"] == pytest.approx(-0.25)
    assert d["incremental_roce"] == pytest.approx((30 - 20) / (190 - 160))
    assert d["interest_burden"] == pytest.approx(0.2) and d["net_debt_to_ebitda"] == pytest.approx(1.0)
    assert d["capex_to_depreciation"] == pytest.approx(70 / 30)
    assert d["share_count_cagr"] == pytest.approx(1.1 ** (1 / 4.0) - 1, rel=1e-3)
    r = app_ratings(_km(), "industrial", fin)
    assert r["munger.quality.revenue_predictability"].rating == 4 and r["buffett.economics.predictability"].rating == 3
    assert r["dalio.credit.credit_dependence"].rating == 4 and r["munger.capital.dilution"].rating == 2  # 2.4% a year
    assert derived_metrics(pd.DataFrame()) == {}

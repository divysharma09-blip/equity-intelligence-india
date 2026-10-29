import pytest

from eii.models import (DataWarning, ScenarioAssumption, VerificationStatus)
from eii.scoring import (DEFAULT_SCORECARD, MetricRule, assess_readiness, compute_scorecard,
                         score_band)
from eii.valuation import (dcf_value, project_scenario, reverse_dcf_implied_growth, run_scenarios,
                           suggest_scenarios)

# ------------------------------------------------------------------ scenarios


def test_project_scenario_arithmetic():
    a = ScenarioAssumption(scenario="base", horizon_years=5, eps_growth=0.10, exit_pe=20, dividend_yield=0.01)
    r = project_scenario(price=200, eps=10, a=a)
    assert r.end_eps == pytest.approx(10 * 1.1 ** 5)
    assert r.target_price == pytest.approx(10 * 1.1 ** 5 * 20)
    assert r.cagr == pytest.approx((r.target_price / 200) ** (1 / 5) - 1 + 0.01)
    assert len(r.eps_path) == 5


def test_project_scenario_rejects_loss_makers():
    a = ScenarioAssumption(scenario="bear", horizon_years=1, eps_growth=0.0, exit_pe=10)
    with pytest.raises(ValueError, match="positive latest EPS"):
        project_scenario(100, -1, a)
    results, errors = run_scenarios(100, None, [a])
    assert results == [] and len(errors) == 1


def test_scenario_horizon_validation():
    with pytest.raises(ValueError):
        ScenarioAssumption(scenario="base", horizon_years=4, eps_growth=0.1, exit_pe=10)


def test_suggest_scenarios_requires_data():
    assert suggest_scenarios(pe=None, eps_cagr=0.1, revenue_cagr=None) == []
    assert suggest_scenarios(pe=20, eps_cagr=None, revenue_cagr=None) == []
    s = suggest_scenarios(pe=20, eps_cagr=0.40, revenue_cagr=None, horizon_years=3)
    assert [x.scenario for x in s] == ["bear", "base", "bull"]
    assert s[1].eps_growth == 0.25  # clipped
    assert all(x.status.value == "assumption" for x in s)
    assert "Starting assumption" in s[1].rationale


def test_dcf_and_reverse_dcf_roundtrip():
    r = dcf_value(fcf0=100, growth=0.10, years=5, terminal_growth=0.04, discount_rate=0.12,
                  net_debt=200, shares=10)
    # independent recomputation
    fcfs = [100 * 1.1 ** t for t in range(1, 6)]
    pv = sum(f / 1.12 ** t for t, f in enumerate(fcfs, start=1))
    tv = fcfs[-1] * 1.04 / (0.12 - 0.04) / 1.12 ** 5
    assert r.enterprise_value == pytest.approx(pv + tv)
    assert r.per_share == pytest.approx((pv + tv - 200) / 10)
    g = reverse_dcf_implied_growth(r.per_share, 100, 5, 0.04, 0.12, 200, 10)
    assert g == pytest.approx(0.10, abs=1e-5)


def test_dcf_input_validation():
    with pytest.raises(ValueError, match="exceed terminal"):
        dcf_value(100, 0.1, 5, 0.12, 0.10, 0, 10)
    assert reverse_dcf_implied_growth(100, -50, 5, 0.04, 0.12, 0, 10) is None

# ------------------------------------------------------------------ scorecard


def test_metric_rule_linear_and_clipped():
    higher = MetricRule(metric="x", label="x", poor=0.0, excellent=0.2)
    assert higher.score(0.1) == 50
    assert higher.score(0.5) == 100 and higher.score(-1) == 0
    lower = MetricRule(metric="pe", label="pe", poor=60, excellent=10)
    assert lower.score(35) == 50
    assert lower.score(5) == 100
    assert lower.score(None) is None


def test_scorecard_missing_metrics_are_excluded_not_zeroed():
    values = {"roe_avg_3y": 0.25, "roce_avg_3y": 0.25}  # only two business-quality metrics
    sc = compute_scorecard(values)
    bq = next(f for f in sc.factors if f.factor == "business_quality")
    assert bq.score == 100 and bq.coverage == 0.5
    assert sc.overall_score == 100  # only factor with data
    assert sc.coverage == pytest.approx(0.5 * 20 / 100)


def test_scorecard_weights_renormalise():
    values = {"roe_avg_3y": 0.25, "roce_avg_3y": 0.25, "ebit_margin_avg_3y": 0.25,
              "ebit_margin_volatility": 0.01, "revenue_cagr": 0.0, "eps_cagr": 0.0}
    sc = compute_scorecard(values)
    assert sc.overall_score == pytest.approx((100 * 20 + 0 * 15) / 35, abs=0.1)
    custom = DEFAULT_SCORECARD.with_weights({"business_quality": 0, "growth": 10})
    assert compute_scorecard(values, custom).overall_score == 0


def test_bank_scorecard_swaps_industrial_for_bank_metrics():
    values = {"debt_to_equity": 8.0, "roe_avg_3y": 0.15, "cet1_ratio": 0.16, "gnpa_pct": 0.01,
              "nnpa_pct": 0.0}
    sc = compute_scorecard(values, company_type="bank")
    fs = next(f for f in sc.factors if f.factor == "financial_strength")
    by_metric = {m.metric: m for m in fs.metrics}
    assert by_metric["debt_to_equity"].excluded_reason and by_metric["debt_to_equity"].score is None
    assert by_metric["cet1_ratio"].score == 100 and by_metric["gnpa_pct"].score == 100
    assert fs.coverage == 1.0  # only bank-applicable metrics count toward coverage
    assert fs.score == 100


def test_industrial_scorecard_ignores_bank_metrics():
    sc = compute_scorecard({"cet1_ratio": 0.2, "debt_to_equity": 0.0}, company_type="industrial")
    fs = next(f for f in sc.factors if f.factor == "financial_strength")
    assert next(m for m in fs.metrics if m.metric == "cet1_ratio").excluded_reason
    assert fs.coverage == pytest.approx(1 / 3)


def test_insurer_is_blocked_by_gate():
    full = compute_scorecard({m.metric: 1.0 for f in DEFAULT_SCORECARD.factors for m in f.metrics})
    r = assess_readiness(VerificationStatus.VERIFIED, 5, full, True, [], company_type="insurer")
    assert not r.can_conclude and "insurers" in r.reasons[0]


def test_infer_company_type():
    from eii.scoring import infer_company_type
    assert infer_company_type("Financial Services", "Banks - Regional") == "bank"
    assert infer_company_type("Financial Services", "Insurance - Life") == "insurer"
    assert infer_company_type("Financial Services", "Credit Services") == "nbfc"
    assert infer_company_type("Technology", "IT Services") == "industrial"
    assert infer_company_type(None, None) == "unknown"


def test_readiness_gate_blocks_incomplete_or_unverified():
    full = compute_scorecard({m.metric: 1.0 for f in DEFAULT_SCORECARD.factors for m in f.metrics})
    ok = assess_readiness(VerificationStatus.VERIFIED, 4, full, True, [])
    assert ok.can_conclude and ok.reasons == []
    bad = assess_readiness(VerificationStatus.PARTIAL, 2, compute_scorecard({}), False,
                           [DataWarning(severity="critical", section="x", message="y")])
    assert not bad.can_conclude
    assert len(bad.reasons) == 5


def test_score_band_has_no_buy_sell_language():
    for s in (None, 10, 50, 65, 90):
        band = score_band(s).lower()
        assert "buy" not in band and "sell" not in band


def test_suggest_scenarios_skips_extreme_pe():
    assert suggest_scenarios(pe=1450, eps_cagr=0.1, revenue_cagr=None) == []

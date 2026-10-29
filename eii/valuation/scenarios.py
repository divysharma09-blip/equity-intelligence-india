"""Bear / base / bull expected-return scenarios.

Model (deliberately simple and fully transparent):
    EPS_t        = EPS_0 × (1 + g)^t
    Target price = EPS_n × exit P/E
    Price CAGR   = (Target / Price_0)^(1/n) − 1
    Expected CAGR ≈ Price CAGR + assumed dividend yield

Every input except today's price and latest reported EPS is an ASSUMPTION
entered (or accepted) by the analyst. Outputs are scenario arithmetic, not forecasts.
"""

from __future__ import annotations

from typing import Optional

from eii.models import DataStatus, ScenarioAssumption, ScenarioResult


def project_scenario(price: float, eps: float, a: ScenarioAssumption) -> ScenarioResult:
    if price is None or price <= 0:
        raise ValueError("Scenario projection needs a positive current price.")
    if eps is None or eps <= 0:
        raise ValueError("Scenario projection needs positive latest EPS; P/E-based scenarios are "
                         "not meaningful for loss-making companies.")
    n = a.horizon_years
    path = [eps * (1 + a.eps_growth) ** t for t in range(1, n + 1)]
    target = path[-1] * a.exit_pe
    price_cagr = (target / price) ** (1 / n) - 1
    expected_cagr = price_cagr + a.dividend_yield
    return ScenarioResult(
        scenario=a.scenario, horizon_years=n, start_price=price, start_eps=eps, end_eps=path[-1],
        target_price=target, total_return=(1 + expected_cagr) ** n - 1, cagr=expected_cagr,
        eps_path=path,
    )


def run_scenarios(price: Optional[float], eps: Optional[float],
                  assumptions: list[ScenarioAssumption]) -> tuple[list[ScenarioResult], list[str]]:
    results, errors = [], []
    for a in assumptions:
        try:
            results.append(project_scenario(price, eps, a))
        except ValueError as exc:
            errors.append(f"{a.scenario} ({a.horizon_years}y): {exc}")
    return results, errors


MAX_ANCHOR_PE = 150.0


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def suggest_scenarios(pe: Optional[float], eps_cagr: Optional[float], revenue_cagr: Optional[float],
                      horizon_years: int = 5, dividend_yield: Optional[float] = None
                      ) -> list[ScenarioAssumption]:
    """Starting-point assumptions derived from the company's own history.

    Returns [] when there is not enough data (no current P/E, or no historical growth).
    These are explicitly ASSUMPTIONS for the analyst to edit, never forecasts.
    """
    hist_growth = eps_cagr if eps_cagr is not None else revenue_cagr
    if pe is None or pe <= 0 or pe > MAX_ANCHOR_PE or hist_growth is None:
        return []  # an extreme current P/E is not a sensible anchor for an exit multiple
    base_g = _clip(hist_growth, -0.05, 0.25)
    dy = _clip(dividend_yield or 0.0, 0.0, 0.10)
    basis = "EPS" if eps_cagr is not None else "revenue"
    note = (f"Auto-suggested from historical {basis} CAGR ({hist_growth:.1%}) and current P/E "
            f"({pe:.1f}x). Starting assumption only — review and edit.")
    return [
        ScenarioAssumption(scenario="bear", horizon_years=horizon_years,
                           eps_growth=round(base_g - 0.05, 4), exit_pe=round(pe * 0.75, 2),
                           dividend_yield=round(dy, 4), rationale=note + " Bear: growth −5pp, P/E −25%.",
                           status=DataStatus.ASSUMPTION),
        ScenarioAssumption(scenario="base", horizon_years=horizon_years,
                           eps_growth=round(base_g, 4), exit_pe=round(pe, 2),
                           dividend_yield=round(dy, 4), rationale=note + " Base: growth and P/E unchanged.",
                           status=DataStatus.ASSUMPTION),
        ScenarioAssumption(scenario="bull", horizon_years=horizon_years,
                           eps_growth=round(base_g + 0.05, 4), exit_pe=round(pe * 1.2, 2),
                           dividend_yield=round(dy, 4), rationale=note + " Bull: growth +5pp, P/E +20%.",
                           status=DataStatus.ASSUMPTION),
    ]

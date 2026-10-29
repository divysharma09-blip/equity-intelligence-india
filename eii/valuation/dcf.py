"""Two-stage free-cash-flow DCF and reverse DCF.

Stage 1: FCF grows at `growth` for `years`. Stage 2: Gordon-growth terminal value.
All inputs are analyst assumptions; the functions only do the arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class DCFResult:
    pv_explicit: float
    pv_terminal: float
    enterprise_value: float
    equity_value: float
    per_share: float

    @property
    def terminal_share(self) -> float:
        return self.pv_terminal / self.enterprise_value if self.enterprise_value else float("nan")


def dcf_value(fcf0: float, growth: float, years: int, terminal_growth: float, discount_rate: float,
              net_debt: float, shares: float) -> DCFResult:
    if years < 1:
        raise ValueError("Explicit forecast period must be at least 1 year.")
    if discount_rate <= terminal_growth:
        raise ValueError("Discount rate must exceed terminal growth rate.")
    if shares <= 0:
        raise ValueError("Shares outstanding must be positive.")
    if discount_rate <= -1 or growth <= -1:
        raise ValueError("Rates must be greater than -100%.")
    pv, fcf = 0.0, fcf0
    for t in range(1, years + 1):
        fcf *= 1 + growth
        pv += fcf / (1 + discount_rate) ** t
    terminal = fcf * (1 + terminal_growth) / (discount_rate - terminal_growth)
    pv_terminal = terminal / (1 + discount_rate) ** years
    ev = pv + pv_terminal
    equity = ev - net_debt
    return DCFResult(pv, pv_terminal, ev, equity, equity / shares)


def reverse_dcf_implied_growth(price: float, fcf0: float, years: int, terminal_growth: float,
                               discount_rate: float, net_debt: float, shares: float,
                               lo: float = -0.5, hi: float = 1.0, tol: float = 1e-6
                               ) -> Optional[float]:
    """Stage-1 FCF growth the current price implies. None if no solution in [lo, hi]
    or FCF is not positive (the relationship is then not monotonic)."""
    if fcf0 <= 0 or price <= 0:
        return None

    def gap(g: float) -> float:
        return dcf_value(fcf0, g, years, terminal_growth, discount_rate, net_debt, shares).per_share - price

    g_lo, g_hi = gap(lo), gap(hi)
    if g_lo > 0 or g_hi < 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if gap(mid) > 0:
            hi = mid
        else:
            lo = mid
        if hi - lo < tol:
            break
    return (lo + hi) / 2

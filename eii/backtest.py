"""Historical backtesting — NOT IMPLEMENTED (planned extension).

This module fixes the contract a future backtester must honour so results are
not flattered by bias. It deliberately refuses to run until a point-in-time
data source exists.

Safeguards the implementation must enforce:
* Look-ahead bias: on each rebalance date only use financial statements whose
  *publication* date (not fiscal period end) is on or before that date, and
  prices on or before it. `eii.calculations.momentum(as_of=...)` already
  respects an `as_of` cutoff.
* Survivorship bias: the universe on each date must come from a historical
  constituent list including companies later delisted, merged or suspended.
* Corporate actions: prices adjusted for splits/bonuses; delisting returns included.
* Costs: brokerage, STT, slippage and impact costs.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field, model_validator


class BacktestConfig(BaseModel):
    start: date
    end: date
    rebalance_months: int = Field(default=12, ge=1)
    horizon_years: int = Field(default=1, ge=1, le=10)
    point_in_time_fundamentals: bool = True
    include_delisted: bool = True
    transaction_cost_bps: float = Field(default=50, ge=0)

    @model_validator(mode="after")
    def _guard(self):
        if self.end <= self.start:
            raise ValueError("Backtest end must be after start.")
        if not (self.point_in_time_fundamentals and self.include_delisted):
            raise ValueError("Backtests must use point-in-time fundamentals and include delisted "
                             "companies to avoid look-ahead and survivorship bias.")
        return self


class BacktestNotAvailable(NotImplementedError):
    pass


def run_backtest(config: BacktestConfig):
    raise BacktestNotAvailable(
        "Backtesting is not implemented: it requires a point-in-time fundamentals source with "
        "publication dates and a survivorship-free historical universe, neither of which is "
        "configured. No backtest results are produced.")

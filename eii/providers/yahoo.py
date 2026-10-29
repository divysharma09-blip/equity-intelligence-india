"""Yahoo Finance fundamentals via the open-source `yfinance` library.

This is an UNOFFICIAL aggregator: it is free, has no SLA, can be rate-limited,
and its figures are not audited. Every value it supplies is tagged with an
`unofficial_aggregator` source and the UI tells the user to cross-check
against the company's annual report / exchange filings before relying on it.

Only the provider's own row names are mapped to canonical metrics below; no
row is ever synthesised from other rows here (that is the job of
`eii.calculations`).
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from eii.models import (CompanyProfile, DataStatus, DataWarning, FinancialFact, MarketSnapshot,
                        SecurityIdentity, Source, SourceKind)
from eii.providers.base import (FundamentalsBundle, FundamentalsProvider, ProviderStatus,
                                ProviderUnavailable)

log = logging.getLogger("eii.providers")

# canonical metric -> candidate provider rows, in order of preference
INCOME_MAP = {
    "revenue": ["Total Revenue", "Operating Revenue"],
    "ebitda": ["EBITDA"],
    "ebit": ["EBIT"],
    "pretax_income": ["Pretax Income"],
    "tax_provision": ["Tax Provision"],
    "net_income": ["Net Income Common Stockholders", "Net Income"],
    "diluted_eps": ["Diluted EPS"],
    "interest_expense": ["Interest Expense", "Interest Expense Non Operating"],
}
BALANCE_MAP = {
    "total_assets": ["Total Assets"],
    "total_equity": ["Stockholders Equity", "Common Stock Equity"],
    "total_debt": ["Total Debt"],
    "cash_and_st_investments": ["Cash Cash Equivalents And Short Term Investments",
                                "Cash And Cash Equivalents"],
    "current_assets": ["Current Assets"],
    "current_liabilities": ["Current Liabilities"],
    "receivables": ["Accounts Receivable", "Receivables"],
    "inventory": ["Inventory"],
    "shares_outstanding": ["Ordinary Shares Number", "Share Issued"],
}
CASHFLOW_MAP = {
    "operating_cash_flow": ["Operating Cash Flow"],
    "capex": ["Capital Expenditure"],
    "free_cash_flow": ["Free Cash Flow"],
    "dividends_paid": ["Cash Dividends Paid"],
}
# Non-currency units. EPS is "<currency>/share"; all other amounts are in the statement currency.
UNITS = {"shares_outstanding": "shares"}


def yahoo_ticker(identity: SecurityIdentity) -> Optional[str]:
    if identity.nse_symbol:
        return f"{identity.nse_symbol}.NS"
    if identity.bse_code:
        return f"{identity.bse_code}.BO"
    return None


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def map_statement(frame: Optional[pd.DataFrame], mapping: dict[str, list[str]], source_id: str,
                  currency: str = "INR") -> tuple[list[FinancialFact], dict[str, str]]:
    """Convert a yfinance statement (rows=line items, columns=period ends) to facts.
    Returns facts and the provider row used for each metric (for transparency)."""
    facts: list[FinancialFact] = []
    used: dict[str, str] = {}
    if frame is None or frame.empty:
        return facts, used
    for metric, candidates in mapping.items():
        row_name = next((r for r in candidates if r in frame.index and frame.loc[r].notna().any()), None)
        if row_name is None:
            continue
        used[metric] = row_name
        for period, value in frame.loc[row_name].items():
            val = _num(value)
            if val is None:
                continue
            unit = UNITS.get(metric) or (f"{currency}/share" if metric == "diluted_eps" else currency)
            facts.append(FinancialFact(metric=metric, period_end=pd.Timestamp(period).date(),
                                       value=val, unit=unit, status=DataStatus.REPORTED,
                                       source_id=source_id))
    return facts, used


class YahooFinanceProvider(FundamentalsProvider):
    name = "Yahoo Finance via yfinance (unofficial)"
    kind = SourceKind.UNOFFICIAL_AGGREGATOR
    description = ("Free, unofficial data. Annual statements (about 4 years), profile, price "
                   "history. Not audited — cross-check with annual reports.")

    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    def status(self) -> ProviderStatus:
        if not self.enabled:
            return ProviderStatus(self.name, False, "Disabled (EII_ENABLE_YFINANCE=false)")
        try:
            import yfinance  # noqa: F401
        except ImportError:
            return ProviderStatus(self.name, False, "yfinance is not installed (pip install yfinance)")
        return ProviderStatus(self.name, True, "Enabled. Data is unofficial and unverified.")

    def fetch(self, identity: SecurityIdentity) -> FundamentalsBundle:
        st = self.status()
        if not st.available:
            raise ProviderUnavailable(st.message)
        import yfinance as yf

        ticker_symbol = yahoo_ticker(identity)
        if not ticker_symbol:
            raise ProviderUnavailable("No NSE symbol or BSE code to query Yahoo Finance with.")
        url = f"https://finance.yahoo.com/quote/{ticker_symbol}"
        now = datetime.now(timezone.utc)
        bundle = FundamentalsBundle()
        ticker = yf.Ticker(ticker_symbol)

        # ---- profile & market snapshot
        try:
            info = ticker.info or {}
        except Exception as exc:  # yfinance raises many exception types on network/rate-limit errors
            log.warning("Yahoo info failed for %s: %s", ticker_symbol, exc)
            info = {}
            bundle.warnings.append(DataWarning(severity="warning", section="Company overview",
                                               message=f"Yahoo Finance profile unavailable: {exc}"))
        currency = info.get("financialCurrency") or "INR"
        if info:
            prof_src = Source(title=f"Yahoo Finance profile — {ticker_symbol}", provider=self.name,
                              kind=self.kind, url=f"{url}/profile", retrieved_at=now,
                              notes="Unofficial. Company description is the provider's text, not verified.")
            bundle.sources.append(prof_src)
            bundle.profile = CompanyProfile(
                description=info.get("longBusinessSummary"), sector=info.get("sector"),
                industry=info.get("industry"), website=info.get("website"),
                provider_name=info.get("longName") or info.get("shortName"),
                provider_symbol=info.get("symbol"), source_id=prof_src.source_id)
            price = _num(info.get("currentPrice")) or _num(info.get("regularMarketPrice"))
            if price is not None:
                mkt_src = Source(title=f"Yahoo Finance quote — {ticker_symbol}", provider=self.name,
                                 kind=self.kind, url=url, retrieved_at=now,
                                 notes="Price may be delayed. Market cap as reported by provider.")
                bundle.sources.append(mkt_src)
                bundle.market = MarketSnapshot(
                    price=price, market_cap=_num(info.get("marketCap")),
                    shares_outstanding=_num(info.get("sharesOutstanding")),
                    currency=info.get("currency"), as_of=now, source_id=mkt_src.source_id)
            if currency != "INR":
                bundle.warnings.append(DataWarning(
                    severity="warning", section="Financial statements",
                    message=f"Provider reports financials in {currency}, not INR."))

        # ---- annual statements
        fin_src = Source(title=f"Yahoo Finance annual financial statements — {ticker_symbol}",
                         provider=self.name, kind=self.kind, url=f"{url}/financials", retrieved_at=now,
                         notes="Unofficial, unaudited aggregation. Verify against annual report.")
        used_rows: dict[str, str] = {}
        for label, getter, mapping in (("income statement", lambda: ticker.income_stmt, INCOME_MAP),
                                       ("balance sheet", lambda: ticker.balance_sheet, BALANCE_MAP),
                                       ("cash-flow statement", lambda: ticker.cashflow, CASHFLOW_MAP)):
            try:
                frame = getter()
            except Exception as exc:
                log.warning("Yahoo %s failed for %s: %s", label, ticker_symbol, exc)
                bundle.warnings.append(DataWarning(severity="warning", section="Financial statements",
                                                   message=f"Yahoo Finance {label} unavailable: {exc}"))
                continue
            facts, used = map_statement(frame, mapping, fin_src.source_id, currency)
            if not facts:
                bundle.warnings.append(DataWarning(severity="warning", section="Financial statements",
                                                   message=f"Yahoo Finance returned no {label} data."))
            bundle.facts.extend(facts)
            used_rows.update(used)
        if bundle.facts:
            fin_src.notes += " Rows used: " + "; ".join(f"{k}<-'{v}'" for k, v in sorted(used_rows.items()))
            bundle.sources.append(fin_src)

        # ---- price history
        try:
            hist = ticker.history(period="2y", auto_adjust=True)
            if hist is not None and not hist.empty:
                ph = hist[["Close"]].rename(columns={"Close": "close"})
                ph.index = pd.to_datetime(ph.index).tz_localize(None)
                bundle.price_history = ph
                bundle.sources.append(Source(
                    title=f"Yahoo Finance price history (2y, split/dividend adjusted) — {ticker_symbol}",
                    provider=self.name, kind=self.kind, url=f"{url}/history", retrieved_at=now))
        except Exception as exc:
            log.warning("Yahoo history failed for %s: %s", ticker_symbol, exc)
            bundle.warnings.append(DataWarning(severity="warning", section="Momentum",
                                               message=f"Price history unavailable: {exc}"))

        if not (bundle.profile or bundle.facts or bundle.price_history is not None):
            raise ProviderUnavailable(f"Yahoo Finance returned no data for {ticker_symbol}. "
                                      "The symbol may be delisted, or the service may be rate-limiting.")
        return bundle

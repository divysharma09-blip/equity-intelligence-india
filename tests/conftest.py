"""Shared fixtures.

All companies and figures here are FICTIONAL test fixtures, used only to
exercise the code paths. They are not real market data.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from eii.config import Settings
from eii.db import ResearchRepository, init_db
from eii.export import WorkbookManager
from eii.models import (CompanyProfile, DataStatus, FinancialFact, MarketSnapshot, SecurityIdentity,
                        Source, SourceKind)


def make_isin(body: str) -> str:
    """Append a valid ISO 6166 check digit to an 11-character body."""
    digits = "".join(str(int(c, 36)) for c in body)
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch) * (2 if i % 2 == 0 else 1)
        total += d - 9 if d > 9 else d
    return body + str((10 - total % 10) % 10)


ALPHA_ISIN = make_isin("INE999A0101")
BETA_ISIN = make_isin("INE999B0101")
GAMMA_ISIN = make_isin("INE999C0101")

NSE_CSV = f"""SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE
ALPHAIND,Alpha Industries Limited,EQ,06-OCT-2008,5,1,{ALPHA_ISIN},5
BETAFIN,Beta Finance Limited,EQ,03-MAY-1995,10,1,{BETA_ISIN},10
ALPHATEX,Alphatex Fabrics Limited,BE,01-JAN-2015,10,1,{GAMMA_ISIN},10
"""

BSE_CSV = f"""Security Code,Issuer Name,Security Id,Security Name,Status,Group,Face Value,ISIN No,Industry,Instrument
599001,Alpha Industries Limited,ALPHAIND,ALPHA INDUSTRIES LTD,Active,A,5.00,{ALPHA_ISIN},Chemicals,Equity
599002,Beta Finance Limited,BETAFIN,BETA FINANCE LTD,Active,B,10.00,{BETA_ISIN},Finance,Equity
599099,Old Delisted Co Limited,OLDCO,OLD DELISTED CO,Delisted,Z,10.00,{make_isin("INE999D0101")},Misc,Equity
"""


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(data_dir=tmp_path / "data", db_path=tmp_path / "data" / "test.db",
                 excel_path=tmp_path / "data" / "Equity_Intelligence_India.xlsx",
                 reference_dir=tmp_path / "data" / "reference", backup_dir=tmp_path / "data" / "backups",
                 log_dir=tmp_path / "logs", enable_yfinance=False, max_backups=5)
    s.ensure_dirs()
    return s


@pytest.fixture
def repo(settings: Settings) -> ResearchRepository:
    init_db(settings.db_path)
    return ResearchRepository(settings.db_path)


@pytest.fixture
def workbook(settings: Settings) -> WorkbookManager:
    return WorkbookManager(settings.excel_path, settings.backup_dir, settings.max_backups)


@pytest.fixture
def nse_df() -> pd.DataFrame:
    from eii.providers.exchange_masters import NSEEquityMaster
    return NSEEquityMaster.parse(NSE_CSV)


@pytest.fixture
def bse_df() -> pd.DataFrame:
    from eii.providers.exchange_masters import BSEScripMaster
    return BSEScripMaster.parse(BSE_CSV)


@pytest.fixture
def alpha_identity() -> SecurityIdentity:
    return SecurityIdentity(company_name="Alpha Industries Limited", nse_symbol="ALPHAIND",
                            bse_code="599001", isin=ALPHA_ISIN, series="EQ", matched_on="nse_symbol")


# Fictional four-year statements (INR). Chosen so ratios are easy to verify by hand.
FIN_DATA = {
    date(2022, 3, 31): dict(revenue=1000, ebitda=250, ebit=200, net_income=120, diluted_eps=12,
                            interest_expense=20, total_assets=1500, total_equity=800, total_debt=300,
                            cash_and_st_investments=100, current_assets=600, current_liabilities=400,
                            operating_cash_flow=150, capex=-50, dividends_paid=-30),
    date(2023, 3, 31): dict(revenue=1100, ebitda=280, ebit=220, net_income=132, diluted_eps=13.2,
                            interest_expense=20, total_assets=1600, total_equity=880, total_debt=300,
                            cash_and_st_investments=120, current_assets=650, current_liabilities=420,
                            operating_cash_flow=160, capex=-60, dividends_paid=-33),
    date(2024, 3, 31): dict(revenue=1210, ebitda=300, ebit=242, net_income=145.2, diluted_eps=14.52,
                            interest_expense=22, total_assets=1700, total_equity=960, total_debt=280,
                            cash_and_st_investments=150, current_assets=700, current_liabilities=430,
                            operating_cash_flow=170, capex=-60, dividends_paid=-36),
    date(2025, 3, 31): dict(revenue=1331, ebitda=330, ebit=266.2, net_income=159.72, diluted_eps=15.972,
                            interest_expense=22, total_assets=1800, total_equity=1050, total_debt=250,
                            cash_and_st_investments=180, current_assets=760, current_liabilities=440,
                            operating_cash_flow=190, capex=-70, dividends_paid=-40),
}


@pytest.fixture
def fin_source() -> Source:
    return Source(source_id="srcfin", title="Test fixture statements", provider="pytest",
                  kind=SourceKind.USER_SUPPLIED, url="https://example.com/fixture")


@pytest.fixture
def facts(fin_source: Source) -> list[FinancialFact]:
    return [FinancialFact(metric=m, period_end=d, value=float(v), status=DataStatus.REPORTED,
                          source_id=fin_source.source_id)
            for d, row in FIN_DATA.items() for m, v in row.items()]


@pytest.fixture
def market() -> MarketSnapshot:
    return MarketSnapshot(price=319.44, market_cap=3194.4, shares_outstanding=10, currency="INR",
                          as_of=datetime(2025, 6, 1, tzinfo=timezone.utc), return_12m=0.1,
                          price_vs_200dma=0.05, source_id="srcmkt")


@pytest.fixture
def profile() -> CompanyProfile:
    return CompanyProfile(description="Fictional chemicals maker used in tests.", sector="Basic Materials",
                          industry="Specialty Chemicals", provider_name="Alpha Industries Ltd",
                          provider_symbol="ALPHAIND.NS", source_id="srcprof")

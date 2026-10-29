"""Builds the configured providers. Register new providers here."""

from __future__ import annotations

from dataclasses import dataclass, field

from eii.config import Settings
from eii.providers.base import FundamentalsProvider, ProviderStatus, SecurityMasterProvider
from eii.providers.exchange_masters import BSEScripMaster, NSEEquityMaster
from eii.providers.nse_filings import NSEClient, NSEFilingsProvider
from eii.providers.yahoo import YahooFinanceProvider


@dataclass
class ProviderRegistry:
    nse: SecurityMasterProvider
    bse: SecurityMasterProvider
    fundamentals: list[FundamentalsProvider] = field(default_factory=list)

    def statuses(self) -> list[ProviderStatus]:
        return [p.status() for p in (self.nse, self.bse, *self.fundamentals)]


def build_registry(settings: Settings) -> ProviderRegistry:
    return ProviderRegistry(
        nse=NSEEquityMaster(settings.nse_master_url, settings.reference_dir,
                            settings.reference_cache_hours, settings.http_timeout_seconds),
        bse=BSEScripMaster(settings.reference_dir),
        # Order = priority for financial statements. Official filings first; Yahoo supplies
        # price, profile and price history, and is the statements fallback.
        fundamentals=[
            NSEFilingsProvider(NSEClient(settings.reference_dir, settings.nse_min_interval_seconds,
                                         settings.http_timeout_seconds, settings.nse_listing_ttl_hours),
                               enabled=settings.enable_nse_filings),
            YahooFinanceProvider(enabled=settings.enable_yfinance),
        ],
    )

"""Data-provider interfaces.

Providers fetch and normalise raw data. They never compute ratios, scores or
valuations, and they never invent values: anything a provider cannot supply is
left out and reported through `warnings` or `ProviderUnavailable`.

To add a provider (e.g. a licensed fundamentals API, Screener export, or a
filings parser), subclass one of the interfaces below and register it in
`eii.providers.registry`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from eii.models import (AuditInfo, CompanyProfile, CompanyType, DataWarning, FinancialFact,
                        MarketSnapshot, OwnershipSnapshot, SecurityIdentity, Source, SourceKind)


class ProviderUnavailable(RuntimeError):
    """The provider cannot serve data right now. The message is shown to the user."""


@dataclass
class ProviderStatus:
    name: str
    available: bool
    message: str


class DataProvider(ABC):
    name: str = "unnamed provider"
    kind: SourceKind = SourceKind.UNOFFICIAL_AGGREGATOR
    description: str = ""

    @abstractmethod
    def status(self) -> ProviderStatus:
        """Cheap availability check (must not download large files)."""


class SecurityMasterProvider(DataProvider):
    """Lists of listed securities: used for search and identity verification."""

    exchange: str = ""

    @abstractmethod
    def load(self) -> pd.DataFrame:
        """Return the normalised master with columns: company_name, symbol, isin, series,
        listing_date, face_value. Raises ProviderUnavailable."""

    @abstractmethod
    def source(self) -> Source:
        """Source record describing the master list that was loaded."""


@dataclass
class FundamentalsBundle:
    profile: Optional[CompanyProfile] = None
    market: Optional[MarketSnapshot] = None
    facts: list[FinancialFact] = field(default_factory=list)
    price_history: Optional[pd.DataFrame] = None  # index: date, column: close
    sources: list[Source] = field(default_factory=list)
    warnings: list[DataWarning] = field(default_factory=list)
    company_type: Optional[CompanyType] = None
    statement_basis: Optional[str] = None
    ownership: Optional[OwnershipSnapshot] = None
    audit: Optional[AuditInfo] = None


class FundamentalsProvider(DataProvider):
    """Company profile, market snapshot, annual financial statements and price history."""

    @abstractmethod
    def fetch(self, identity: SecurityIdentity) -> FundamentalsBundle:
        """Raises ProviderUnavailable if nothing could be retrieved."""

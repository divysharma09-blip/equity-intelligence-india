"""Pydantic domain models.

Every figure carries a `DataStatus` so the UI and exports can always tell the
user whether a number was reported by a provider, entered by the analyst,
derived by this app, or is an assumption. Missing data stays `None` — it is
never filled with a placeholder number.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timezone
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex[:16]


class DataStatus(str, Enum):
    REPORTED = "reported"                    # as published by a data provider
    USER_ENTERED = "user_entered"            # typed in by the analyst
    DERIVED = "derived"                      # calculated by this app from other figures
    ESTIMATE = "estimate"                    # an estimate (e.g. consensus) — labelled as such
    ASSUMPTION = "assumption"                # analyst/model assumption, not data
    AI_INTERPRETATION = "ai_interpretation"  # reserved for future LLM summaries
    MISSING = "missing"


DATA_STATUS_LABELS = {
    DataStatus.REPORTED: "Reported by provider",
    DataStatus.USER_ENTERED: "Analyst-entered",
    DataStatus.DERIVED: "Derived (calculated here)",
    DataStatus.ESTIMATE: "Estimate",
    DataStatus.ASSUMPTION: "Assumption",
    DataStatus.AI_INTERPRETATION: "AI-generated interpretation",
    DataStatus.MISSING: "Missing",
}


class SourceKind(str, Enum):
    OFFICIAL_EXCHANGE = "official_exchange"
    COMPANY_FILING = "company_filing"
    UNOFFICIAL_AGGREGATOR = "unofficial_aggregator"
    USER_SUPPLIED = "user_supplied"
    APP_CALCULATION = "app_calculation"


class Source(BaseModel):
    source_id: str = Field(default_factory=new_id)
    title: str
    provider: str
    kind: SourceKind
    url: Optional[str] = None
    retrieved_at: datetime = Field(default_factory=utcnow)
    notes: str = ""

    @field_validator("url")
    @classmethod
    def _valid_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        v = v.strip()
        if not re.match(r"^https?://[^\s]+$", v):
            raise ValueError("Source URL must be a full http(s) URL")
        return v


# --------------------------------------------------------------------------- identity

ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


class SecurityIdentity(BaseModel):
    company_name: str
    nse_symbol: Optional[str] = None
    bse_code: Optional[str] = None
    isin: Optional[str] = None
    series: Optional[str] = None
    listing_date: Optional[date] = None
    face_value: Optional[float] = None
    matched_on: str = ""  # how the search found it: isin / nse_symbol / bse_code / name

    @field_validator("isin", "nse_symbol", "bse_code", "series", mode="before")
    @classmethod
    def _clean_codes(cls, v):
        if v is None:
            return None
        v = str(v).strip().upper()
        return v or None

    @property
    def key(self) -> str:
        """Stable identifier used across SQLite and Excel. ISIN preferred."""
        if self.isin:
            return self.isin
        if self.nse_symbol:
            return f"NSE:{self.nse_symbol}"
        if self.bse_code:
            return f"BSE:{self.bse_code}"
        raise ValueError("Identity has no ISIN, NSE symbol or BSE code")


class VerificationStatus(str, Enum):
    VERIFIED = "verified"
    PARTIAL = "partially_verified"
    UNVERIFIED = "unverified"
    CONFLICT = "conflict"


class VerificationCheck(BaseModel):
    name: str
    passed: Optional[bool]  # None = could not be checked
    detail: str


class VerificationResult(BaseModel):
    status: VerificationStatus
    checks: list[VerificationCheck] = Field(default_factory=list)
    summary: str = ""
    checked_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- data

class FinancialFact(BaseModel):
    metric: str
    period_end: date
    value: Optional[float]
    unit: str = "INR"
    status: DataStatus = DataStatus.REPORTED
    source_id: Optional[str] = None


class CompanyProfile(BaseModel):
    description: Optional[str] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    website: Optional[str] = None
    provider_name: Optional[str] = None  # the provider's own company name, used for identity checks
    provider_symbol: Optional[str] = None
    source_id: Optional[str] = None


class MarketSnapshot(BaseModel):
    price: Optional[float] = None
    market_cap: Optional[float] = None
    shares_outstanding: Optional[float] = None
    currency: Optional[str] = None
    as_of: Optional[datetime] = None
    return_6m: Optional[float] = None
    return_12m: Optional[float] = None
    price_vs_200dma: Optional[float] = None
    high_52w: Optional[float] = None
    low_52w: Optional[float] = None
    source_id: Optional[str] = None


CompanyType = Literal["industrial", "bank", "nbfc", "insurer", "unknown"]


class OwnershipSnapshot(BaseModel):
    """Promoter holding and pledge as disclosed to NSE (shareholding pattern / SAST pledge data)."""
    as_of: date
    promoter_holding_pct: Optional[float] = None
    public_holding_pct: Optional[float] = None
    promoter_pledged_pct_of_promoter: Optional[float] = None  # % of promoter shares that are pledged
    promoter_pledged_pct_of_total: Optional[float] = None     # pledged promoter shares as % of all shares
    promoter_history: list[tuple[date, float]] = Field(default_factory=list)
    source_id: Optional[str] = None


class AuditInfo(BaseModel):
    """Statutory auditor and audit opinion as tagged in the latest annual results filing."""
    period_end: date
    auditors: list[str] = Field(default_factory=list)
    opinion: Optional[str] = None
    source_id: Optional[str] = None

    @property
    def opinion_unmodified(self) -> Optional[bool]:
        if not self.opinion:
            return None
        return "unmodified" in self.opinion.lower()


class DataWarning(BaseModel):
    severity: Literal["info", "warning", "critical"]
    section: str
    message: str
    origin: Literal["provider", "analysis"] = "provider"  # analysis warnings are regenerated


# --------------------------------------------------------------------------- analyst inputs

class Risk(BaseModel):
    category: str
    description: str
    severity: Literal["low", "medium", "high"] = "medium"
    mitigation: str = ""
    status: DataStatus = DataStatus.USER_ENTERED


class GovernanceInputs(BaseModel):
    """Analyst-entered governance facts and judgements. All optional; None = not assessed."""
    promoter_holding_pct: Optional[float] = Field(default=None, ge=0, le=100)
    promoter_pledge_pct: Optional[float] = Field(default=None, ge=0, le=100)
    auditor: str = ""
    governance_rating: Optional[int] = Field(default=None, ge=1, le=5)
    macro_resilience_rating: Optional[int] = Field(default=None, ge=1, le=5)
    notes: str = ""


HORIZONS = (1, 3, 5, 10)
ScenarioName = Literal["bear", "base", "bull"]


class ScenarioAssumption(BaseModel):
    scenario: ScenarioName
    horizon_years: int = 5
    eps_growth: float = Field(ge=-0.9, le=2.0, description="Annual EPS growth, decimal")
    exit_pe: float = Field(gt=0, le=500)
    dividend_yield: float = Field(default=0.0, ge=0, le=0.5)
    rationale: str = ""
    status: DataStatus = DataStatus.ASSUMPTION

    @field_validator("horizon_years")
    @classmethod
    def _horizon(cls, v: int) -> int:
        if v not in HORIZONS:
            raise ValueError(f"Horizon must be one of {HORIZONS}")
        return v


class ScenarioResult(BaseModel):
    scenario: ScenarioName
    horizon_years: int
    start_price: float
    start_eps: float
    end_eps: float
    target_price: float
    total_return: float
    cagr: float
    eps_path: list[float] = Field(default_factory=list)


# --------------------------------------------------------------------------- scoring

class MetricScore(BaseModel):
    metric: str
    label: str
    value: Optional[float]
    score: Optional[float]  # 0..100, None when value missing or excluded
    rule: str
    excluded_reason: Optional[str] = None


class FactorScore(BaseModel):
    factor: str
    label: str
    weight: float
    score: Optional[float]
    coverage: float  # share of the factor's metrics that had data
    metrics: list[MetricScore] = Field(default_factory=list)


class ScorecardResult(BaseModel):
    overall_score: Optional[float]
    coverage: float
    factors: list[FactorScore]
    computed_at: datetime = Field(default_factory=utcnow)


class Readiness(BaseModel):
    can_conclude: bool
    reasons: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- report

class ResearchReport(BaseModel):
    report_id: str = Field(default_factory=new_id)
    identity: SecurityIdentity
    verification: Optional[VerificationResult] = None
    profile: Optional[CompanyProfile] = None
    market: Optional[MarketSnapshot] = None
    financials: list[FinancialFact] = Field(default_factory=list)
    overview_notes: str = ""
    business_model: str = ""
    competitive_advantages: str = ""
    governance: GovernanceInputs = Field(default_factory=GovernanceInputs)
    company_type: CompanyType = "unknown"
    statement_basis: Optional[str] = None  # "Consolidated" / "Standalone"
    ownership: Optional[OwnershipSnapshot] = None
    audit: Optional[AuditInfo] = None
    risks: list[Risk] = Field(default_factory=list)
    scenarios: list[ScenarioAssumption] = Field(default_factory=list)
    scenario_results: list[ScenarioResult] = Field(default_factory=list)
    scorecard: Optional[ScorecardResult] = None
    readiness: Optional[Readiness] = None
    sources: list[Source] = Field(default_factory=list)
    warnings: list[DataWarning] = Field(default_factory=list)
    status: Literal["draft", "completed"] = "draft"
    analyst: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def source_by_id(self, source_id: Optional[str]) -> Optional[Source]:
        return next((s for s in self.sources if s.source_id == source_id), None)

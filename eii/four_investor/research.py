"""Saved Claude Code research runs: one JSON file per company under data/four_investor/.

The files are committed with the app, so a run made on the Mac shows up on the deployed app.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from eii.four_investor.framework import Confidence, Rating

Verdict = Literal["STRONG BUY", "BUY", "ACCUMULATE", "HOLD", "WATCHLIST", "AVOID", "SELL / REDUCE",
                  "INSUFFICIENT DATA"]


class ParamAssessment(BaseModel):
    param_id: str
    rating: Optional[int] = Field(None, ge=0, le=4, description="0–4, or null when the evidence is unavailable")
    observed: str = Field("", description="Actual observed value or finding")
    benchmark: str = Field("", description="Benchmark or sector comparison used")
    evidence: str = Field("", description="Evidence with source and date")
    explanation: str = ""
    confidence: Optional[Confidence] = None
    data_status: Literal["Verified", "Calculated from verified data", "Estimated", "Qualitative assessment",
                         "Unavailable"] = "Unavailable"


class RedFlag(BaseModel):
    issue: str
    severity: Literal["Critical", "High", "Medium", "Low"]
    status: Literal["Resolved", "Unresolved", "Unclear"] = "Unclear"
    source: str = ""


class ValuationRange(BaseModel):
    bear: Optional[float] = None
    base: Optional[float] = None
    bull: Optional[float] = None
    preferred_buy_price: Optional[float] = None
    overvaluation_threshold: Optional[float] = None
    method_note: str = ""


class SourceLink(BaseModel):
    title: str
    url: str = ""


class ClaudeAssessment(BaseModel):
    """What Claude Code returns (the JSON schema passed to the CLI)."""
    assessments: list[ParamAssessment]
    business_quality_score: Optional[float] = Field(None, ge=0, le=100)
    valuation_attractiveness_score: Optional[float] = Field(None, ge=0, le=100)
    risk_score: Optional[float] = Field(None, ge=0, le=100, description="0 = lowest risk, 100 = highest")
    dalio_macro_resilience_score: Optional[float] = Field(None, ge=0, le=100)
    dalio_current_macro_environment_score: Optional[float] = Field(None, ge=0, le=100)
    intrinsic_value_per_share: ValuationRange = Field(default_factory=ValuationRange)
    red_flags: list[RedFlag] = Field(default_factory=list)
    verdict: Verdict = "INSUFFICIENT DATA"
    verdict_summary: str = ""
    report_markdown: str = Field(description="The full 18-section report in Markdown")
    sources: list[SourceLink] = Field(default_factory=list)


class ResearchRun(BaseModel):
    """A saved run: Claude's assessment plus what it was based on."""
    identity_key: str
    company_name: str
    nse_symbol: Optional[str] = None
    generated_at: datetime
    model: str
    horizon: str
    price_used: Optional[float] = None
    app_ratings: dict[str, Rating] = Field(default_factory=dict)
    assessment: ClaudeAssessment

    def claude_ratings(self) -> dict[str, Rating]:
        return {a.param_id: Rating(param_id=a.param_id, rating=a.rating, observed=a.observed,
                                   evidence=" ".join(x for x in [a.evidence, a.explanation] if x),
                                   confidence=a.confidence, source="claude")
                for a in self.assessment.assessments}


def research_dir(data_dir: Path) -> Path:
    return data_dir / "four_investor"


def _path(data_dir: Path, identity_key: str) -> Path:
    return research_dir(data_dir) / f"{identity_key}.json"


def load_run(data_dir: Path, identity_key: str) -> Optional[ResearchRun]:
    path = _path(data_dir, identity_key)
    if not path.exists():
        return None
    return ResearchRun.model_validate_json(path.read_text(encoding="utf-8"))


def save_run(data_dir: Path, run: ResearchRun) -> Path:
    path = _path(data_dir, run.identity_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_text(run.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(path)
    return path

"""Formatting helpers and small reusable widgets."""

from __future__ import annotations

import math
from typing import Optional

import streamlit as st

from eii.config import FRAMEWORK_DISCLAIMER
from eii.models import DATA_STATUS_LABELS, Readiness, ResearchReport, VerificationStatus

CRORE = 1e7
MISSING = "—"

VERIFICATION_BADGE = {
    VerificationStatus.VERIFIED: ("green", ":material/verified: Verified"),
    VerificationStatus.PARTIAL: ("orange", ":material/rule: Partially verified"),
    VerificationStatus.UNVERIFIED: ("gray", ":material/help: Unverified"),
    VerificationStatus.CONFLICT: ("red", ":material/error: Identity conflict"),
}


def _bad(x) -> bool:
    return x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x)))


def fmt_cr(x: Optional[float], decimals: int = 0) -> str:
    return MISSING if _bad(x) else f"₹{x / CRORE:,.{decimals}f} Cr"


def fmt_inr(x: Optional[float], decimals: int = 2) -> str:
    return MISSING if _bad(x) else f"₹{x:,.{decimals}f}"


def fmt_pct(x: Optional[float], decimals: int = 1) -> str:
    return MISSING if _bad(x) else f"{x:.{decimals}%}"


def fmt_x(x: Optional[float], decimals: int = 1) -> str:
    return MISSING if _bad(x) else f"{x:,.{decimals}f}x"


def fmt_num(x: Optional[float], decimals: int = 2) -> str:
    return MISSING if _bad(x) else f"{x:,.{decimals}f}"


def fmt_by_unit(x: Optional[float], unit: str) -> str:
    return {"pct": fmt_pct, "x": fmt_x}.get(unit, fmt_num)(x)


def verification_badge(report: ResearchReport) -> None:
    status = report.verification.status if report.verification else VerificationStatus.UNVERIFIED
    color, label = VERIFICATION_BADGE[status]
    st.badge(label, color=color)


def readiness_banner(readiness: Optional[Readiness]) -> None:
    if readiness and readiness.can_conclude:
        st.success("Data sufficiency gate passed — framework conclusions may be shown. "
                   "This is still not investment advice.", icon=":material/task_alt:")
    else:
        reasons = readiness.reasons if readiness else ["Not assessed"]
        st.warning("**Insufficient verified data — no conclusion or recommendation is shown.**\n\n"
                   + "\n".join(f"- {r}" for r in reasons), icon=":material/block:")


def disclaimer() -> None:
    st.caption(f":material/info: {FRAMEWORK_DISCLAIMER}")


def status_legend() -> None:
    st.caption("Data labels: " + " · ".join(f"**{v}**" for v in DATA_STATUS_LABELS.values()))


def no_report_message() -> None:
    st.info("No company open. Use **Search** to find and open a company first.", icon=":material/search:")
    st.page_link("eii/ui/views/search.py", label="Go to search", icon=":material/search:")

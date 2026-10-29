"""Company / exchange identity verification.

A name match alone is never treated as proof of identity. A company is
VERIFIED only when its ISIN is valid, it appears in an official exchange list
with the same ISIN for the chosen symbol/code, and any fundamentals provider in
use reports a matching company name and ticker.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Optional

import pandas as pd

from eii.models import (ISIN_RE, CompanyProfile, SecurityIdentity, VerificationCheck,
                        VerificationResult, VerificationStatus)

_STOPWORDS = {"limited", "ltd", "the", "pvt", "private", "co", "company", "inc", "corp",
              "corporation", "plc"}


def isin_is_valid(isin: Optional[str]) -> bool:
    """ISO 6166 format + Luhn check digit."""
    if not isin or not ISIN_RE.match(isin):
        return False
    digits = "".join(str(int(c, 36)) for c in isin[:-1])
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - total % 10) % 10 == int(isin[-1])


def normalize_name(name: Optional[str]) -> str:
    if not name:
        return ""
    s = name.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(t for t in s.split() if t not in _STOPWORDS)


def name_similarity(a: Optional[str], b: Optional[str]) -> float:
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    seq = SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    jaccard = len(ta & tb) / len(ta | tb)
    return max(seq, jaccard)


NAME_MATCH = 0.85
NAME_MISMATCH = 0.6


def verify_identity(identity: SecurityIdentity, nse: Optional[pd.DataFrame],
                    bse: Optional[pd.DataFrame] = None,
                    profile: Optional[CompanyProfile] = None,
                    expected_provider_symbol: Optional[str] = None) -> VerificationResult:
    checks: list[VerificationCheck] = []
    conflict = False

    # 1. ISIN validity
    if identity.isin:
        ok = isin_is_valid(identity.isin)
        checks.append(VerificationCheck(name="ISIN format and check digit", passed=ok,
                                        detail=identity.isin if ok else f"{identity.isin} fails ISO 6166 check"))
        conflict |= not ok
        if ok and not identity.isin.startswith("INE"):
            checks.append(VerificationCheck(
                name="Indian equity ISIN prefix (INE)", passed=False,
                detail=f"{identity.isin[:3]} is not an Indian company equity ISIN (e.g. INF = fund units)."))
    else:
        checks.append(VerificationCheck(name="ISIN format and check digit", passed=None,
                                        detail="No ISIN available"))

    # 2. NSE official list
    on_exchange = False
    if identity.nse_symbol:
        if nse is None:
            checks.append(VerificationCheck(name="Listed on NSE (official list)", passed=None,
                                            detail="NSE list unavailable"))
        else:
            rows = nse[nse["symbol"] == identity.nse_symbol]
            if rows.empty:
                checks.append(VerificationCheck(name="Listed on NSE (official list)", passed=False,
                                                detail=f"{identity.nse_symbol} not in NSE equity list"))
            else:
                on_exchange = True
                row = rows.iloc[0]
                checks.append(VerificationCheck(name="Listed on NSE (official list)", passed=True,
                                                detail=f"{identity.nse_symbol}: {row['company_name']}"))
                isin_ok = bool(identity.isin) and row["isin"] == identity.isin
                checks.append(VerificationCheck(name="NSE symbol ↔ ISIN match", passed=isin_ok,
                                                detail=f"NSE list ISIN {row['isin']}"))
                conflict |= not isin_ok
                sim = name_similarity(identity.company_name, row["company_name"])
                checks.append(VerificationCheck(name="Company name matches NSE list",
                                                passed=sim >= NAME_MATCH,
                                                detail=f"similarity {sim:.2f}"))

    # 3. BSE list
    if identity.bse_code:
        if bse is None:
            checks.append(VerificationCheck(name="BSE code ↔ ISIN match", passed=None,
                                            detail="BSE scrip list not configured"))
        else:
            rows = bse[bse["symbol"] == identity.bse_code]
            if rows.empty:
                checks.append(VerificationCheck(name="BSE code ↔ ISIN match", passed=False,
                                                detail=f"{identity.bse_code} not in BSE list"))
                conflict = True
            else:
                on_exchange = True
                ok = bool(identity.isin) and rows.iloc[0]["isin"] == identity.isin
                checks.append(VerificationCheck(name="BSE code ↔ ISIN match", passed=ok,
                                                detail=f"BSE list ISIN {rows.iloc[0]['isin']}"))
                conflict |= not ok
    else:
        checks.append(VerificationCheck(name="BSE code ↔ ISIN match", passed=None,
                                        detail="No BSE code known (BSE list not configured or not listed)"))

    # 4. Fundamentals provider refers to the same company
    provider_uncertain = False
    if profile is not None:
        if expected_provider_symbol and profile.provider_symbol:
            ok = profile.provider_symbol.upper() == expected_provider_symbol.upper()
            checks.append(VerificationCheck(name="Data provider ticker matches", passed=ok,
                                            detail=f"provider {profile.provider_symbol}, expected {expected_provider_symbol}"))
            conflict |= not ok
        sim = name_similarity(identity.company_name, profile.provider_name) if profile.provider_name else 0.0
        if not profile.provider_name:
            # Provider returned no profile (e.g. Yahoo blocking cloud hosts): absence is not a mismatch.
            checks.append(VerificationCheck(name="Data provider company name matches", passed=None,
                                            detail="Provider returned no company name"))
        elif sim >= NAME_MATCH:
            checks.append(VerificationCheck(name="Data provider company name matches", passed=True,
                                            detail=f"'{profile.provider_name}' (similarity {sim:.2f})"))
        elif sim < NAME_MISMATCH:
            checks.append(VerificationCheck(name="Data provider company name matches", passed=False,
                                            detail=f"'{profile.provider_name}' (similarity {sim:.2f}) — provider data may "
                                                   "belong to a different company"))
            conflict = True
        else:
            provider_uncertain = True
            checks.append(VerificationCheck(name="Data provider company name matches", passed=None,
                                            detail=f"'{profile.provider_name}' (similarity {sim:.2f}) — review manually"))

    if conflict:
        status, summary = VerificationStatus.CONFLICT, "Identity checks disagree. Do not rely on this data until resolved."
    elif not on_exchange:
        status, summary = VerificationStatus.UNVERIFIED, "Not found in any official exchange list available to the app."
    elif provider_uncertain or not identity.isin:
        status, summary = VerificationStatus.PARTIAL, "Listed on exchange, but some checks could not be completed."
    else:
        status, summary = VerificationStatus.VERIFIED, "Exchange listing, ISIN and data-provider identity agree."
    return VerificationResult(status=status, checks=checks, summary=summary)

"""Company search across the NSE and BSE security masters."""

from __future__ import annotations

import difflib
from typing import Literal, Optional

import pandas as pd

from eii.models import ISIN_RE, SecurityIdentity
from eii.verification import normalize_name

QueryType = Literal["isin", "bse_code", "text"]


def classify_query(query: str) -> QueryType:
    q = query.strip().upper()
    if ISIN_RE.match(q):
        return "isin"
    if q.isdigit() and len(q) == 6:
        return "bse_code"
    return "text"


def _bse_code_for(isin: Optional[str], bse: Optional[pd.DataFrame]) -> Optional[str]:
    if bse is None or not isin:
        return None
    rows = bse[bse["isin"] == isin]
    return str(rows.iloc[0]["symbol"]) if not rows.empty else None


def _from_nse(row: pd.Series, bse: Optional[pd.DataFrame], matched_on: str) -> SecurityIdentity:
    listing = row.get("listing_date")
    return SecurityIdentity(
        company_name=row["company_name"], nse_symbol=row["symbol"], isin=row["isin"],
        bse_code=_bse_code_for(row["isin"], bse), series=row.get("series"),
        listing_date=listing if pd.notna(listing) else None,
        face_value=float(row["face_value"]) if pd.notna(row.get("face_value")) else None,
        matched_on=matched_on)


def _from_bse(row: pd.Series, matched_on: str) -> SecurityIdentity:
    return SecurityIdentity(company_name=row["company_name"], bse_code=row["symbol"], isin=row["isin"],
                            series=row.get("series"), matched_on=matched_on)


def search_securities(query: str, nse: Optional[pd.DataFrame], bse: Optional[pd.DataFrame] = None,
                      limit: int = 25) -> list[SecurityIdentity]:
    """Return candidate securities. Results are candidates only — identity must still be verified."""
    q = query.strip()
    if not q:
        return []
    qtype = classify_query(q)
    qu = q.upper()
    results: list[SecurityIdentity] = []
    seen: set[str] = set()

    def add(identity: SecurityIdentity) -> None:
        if identity.key not in seen:
            seen.add(identity.key)
            results.append(identity)

    if qtype == "isin":
        if nse is not None:
            for _, r in nse[nse["isin"] == qu].iterrows():
                add(_from_nse(r, bse, "isin"))
        if bse is not None:
            for _, r in bse[bse["isin"] == qu].iterrows():
                add(_from_bse(r, "isin"))
        return results[:limit]

    if qtype == "bse_code":
        if bse is not None:
            for _, r in bse[bse["symbol"] == qu].iterrows():
                nse_rows = nse[nse["isin"] == r["isin"]] if nse is not None else pd.DataFrame()
                add(_from_nse(nse_rows.iloc[0], bse, "bse_code") if not nse_rows.empty
                    else _from_bse(r, "bse_code"))
        return results[:limit]

    if nse is not None and not nse.empty:
        exact = nse[nse["symbol"] == qu]
        for _, r in exact.iterrows():
            add(_from_nse(r, bse, "nse_symbol"))
        for _, r in nse[nse["symbol"].str.startswith(qu, na=False) & (nse["symbol"] != qu)].head(limit).iterrows():
            add(_from_nse(r, bse, "nse_symbol_prefix"))
        nq = normalize_name(q)
        if nq:
            names = nse["company_name"].map(normalize_name)
            contains = nse[names.str.contains(nq, regex=False, na=False)]
            # prefer names that start with the query
            contains = contains.assign(_starts=names[contains.index].str.startswith(nq)).sort_values(
                ["_starts", "company_name"], ascending=[False, True])
            for _, r in contains.head(limit).iterrows():
                add(_from_nse(r, bse, "name"))
            if len(results) < 5:
                close = difflib.get_close_matches(nq, names.tolist(), n=5, cutoff=0.75)
                for nm in close:
                    for _, r in nse[names == nm].iterrows():
                        add(_from_nse(r, bse, "name_fuzzy"))
    if bse is not None and len(results) < limit:
        nq = normalize_name(q)
        names = bse["company_name"].map(normalize_name)
        nse_isins = set(nse["isin"]) if nse is not None else set()
        for _, r in bse[names.str.contains(nq, regex=False, na=False)].head(limit).iterrows():
            if r["isin"] not in nse_isins:
                add(_from_bse(r, "name"))
    return results[:limit]

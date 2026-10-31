"""Public evidence for the four-investor parameters, collected from official NSE sources.

All sources work without a login and from cloud hosts (verified 2026-10-31):

* NSE index constituent lists (nsearchives)   sector ("Industry") and index tier (scale)
* BRSR XBRL, via /api/corporate-bussiness-sustainabilitiy
                                              exports, R&D, related-party share, attrition,
                                              dealer concentration, locations, MSME sourcing
* Corporate announcements, 3 years            red flags (fraud/default/insolvency), auditor
                                              resignations, investor communication,
                                              acquisitions, buybacks, credit-rating actions
* Shareholding pattern history                promoter holding trend
* Bhavcopy closing prices at each financial-year end   historical P/E

Every value is reported with its source; anything that cannot be fetched is simply absent.
"""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Optional

import pandas as pd
import requests

from eii.models import FinancialFact, SecurityIdentity, Source, SourceKind
from eii.providers.base import ProviderUnavailable
from eii.providers.nse_bhavcopy import NSEBhavcopyProvider
from eii.providers.nse_filings import BASE, NSEClient

log = logging.getLogger("eii.four_investor")

INDEX_LISTS = {  # tier: (label, file); tiers partition the Nifty Total Market (750 stocks)
    4: ("Nifty 100", "ind_nifty100list.csv"),
    3: ("Nifty Midcap 150", "ind_niftymidcap150list.csv"),
    2: ("Nifty Smallcap 250", "ind_niftysmallcap250list.csv"),
    1: ("Nifty Microcap 250", "ind_niftymicrocap250_list.csv"),
}
TOTAL_MARKET = "ind_niftytotalmarket_list.csv"
INDEX_URL = "https://nsearchives.nseindia.com/content/indices/{}"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

INVESTOR_COMMS = re.compile(r"investor presentation|earnings call|conference call|con\. ?call|concall|"
                            r"transcript|analysts?/institutional investor meet|analyst meet", re.I)
RED_FLAG_DESCS = ("Fraud/Default/Arrest", "Corporate Insolvency Resolution Process", "Default")


@dataclass
class Evidence:
    values: dict[str, float] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)
    sector: Optional[str] = None
    sector_source: str = ""
    index_tier: Optional[int] = None
    sources: list[Source] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def put(self, key: str, value: Optional[float], note: str = "") -> None:
        if value is not None:
            self.values[key] = float(value)
            if note:
                self.notes[key] = note


# ---------------------------------------------------------------------------- index lists

def _cached_csv(cache_dir: Path, name: str, max_age_hours: float = 24, timeout: int = 20) -> pd.DataFrame:
    path = cache_dir / "indices" / name
    if not (path.exists() and time.time() - path.stat().st_mtime < max_age_hours * 3600):
        try:
            resp = requests.get(INDEX_URL.format(name), headers={"User-Agent": _UA}, timeout=timeout)
            resp.raise_for_status()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(resp.content)
        except requests.RequestException as exc:
            if not path.exists():
                raise ProviderUnavailable(f"NSE index list {name} unavailable: {exc}") from exc
            log.warning("Using stale %s (%s)", name, exc)
    return pd.read_csv(path, dtype=str)


def _sector_and_tier(ev: Evidence, identity: SecurityIdentity, cache_dir: Path) -> None:
    def match(df: pd.DataFrame) -> pd.DataFrame:
        hit = df[df["ISIN Code"].str.strip() == identity.isin] if identity.isin else df.iloc[0:0]
        return hit if not hit.empty else df[df["Symbol"].str.strip() == (identity.nse_symbol or "")]

    try:
        tm = match(_cached_csv(cache_dir, TOTAL_MARKET))
        if not tm.empty:
            ev.sector = tm.iloc[0]["Industry"].strip()
            ev.sector_source = "NSE Nifty Total Market constituent list"
        for tier, (label, name) in INDEX_LISTS.items():
            if not match(_cached_csv(cache_dir, name)).empty:
                ev.index_tier = tier
                ev.put("index_tier", tier, f"Constituent of the {label} index.")
                break
        if ev.index_tier is None:
            ev.put("index_tier", 0, "Not in the Nifty Total Market (750 largest listed companies).")
        ev.sources.append(Source(title="NSE index constituent lists (Nifty Total Market, 100, Midcap 150, "
                                       "Smallcap 250, Microcap 250)", provider="NSE Indices",
                                 kind=SourceKind.OFFICIAL_EXCHANGE, url=INDEX_URL.format(TOTAL_MARKET)))
    except ProviderUnavailable as exc:
        ev.errors.append(str(exc))


# ---------------------------------------------------------------------------- BRSR

def parse_brsr(content: bytes) -> dict[str, float]:
    """Latest-year values from a BRSR XBRL instance. Percentages are fractions, as filed."""
    root = ET.fromstring(content)
    xi = "{http://www.xbrl.org/2003/instance}"
    ctx: dict[str, tuple[Optional[str], frozenset]] = {}
    for c in root.iter(f"{xi}context"):
        per = c.find(f"{xi}period")
        end = None if per is None else (getattr(per.find(f"{xi}endDate"), "text", None)
                                        or getattr(per.find(f"{xi}instant"), "text", None))
        dims = frozenset(m.text.split(":")[-1] for m in c.iter() if m.tag.endswith("explicitMember") and m.text)
        typed = any(m.tag.endswith("typedMember") for m in c.iter())
        ctx[c.get("id")] = (end, dims | ({"__typed__"} if typed else set()))
    facts: list[tuple[str, Optional[str], frozenset, float]] = []
    for el in root:
        ref, text = el.get("contextRef"), (el.text or "").strip()
        if ref is None or not text:
            continue
        try:
            num = float(text)
        except ValueError:
            continue
        end, dims = ctx.get(ref, (None, frozenset()))
        facts.append((el.tag.rsplit("}", 1)[-1], end, dims, num))
    ends = sorted({e for _, e, _, _ in facts if e})
    if not ends:
        return {}
    latest = ends[-1]

    def get(name: str, *dims: str, total: bool = False) -> Optional[float]:
        vals = [v for n, e, d, v in facts if n == name and e == latest and set(dims) <= d and
                (dims or not d or total)]
        return sum(vals) if total and vals else (vals[0] if vals else None)

    out = {
        "exports_pct": get("PercentageOfContributionOfExportsInTheTotalTurnoverOfTheEntity"),
        "rnd_pct": get("PercentageOfRAndD"),
        "rpt_purchases_pct": get("PercentageOfPurchasesFromRelatedPartiesInTotalPurchasesForShareOfRelatedPartyTransactions"),
        "rpt_sales_pct": get("PercentageOfSalesToRelatedPartiesInTotalSalesForShareOfRelatedPartyTransactions"),
        "rpt_loans_pct": get("PercentageOfLoansAndAdvancesGivenToRelatedPartiesInTotalLoansAndAdvances"),
        "rpt_investments_pct": get("PercentageOfInvestmentsInRelatedPartiesInTotalInvestments"),
        "top10_dealer_pct": get("PercentageOfSalesToTopTenDealersOrDistributorsInTotalSalesToDealersOrDistributors"),
        "dealers_count": get("NumberOfDealersOrDistributorsToWhomSalesAreMade"),
        "msme_sourcing_pct": get("PercentageOfDirectlySourcedFromMSMEsOrSmallProducers"),
        "locations_national": get("NumberOfLocations", "LocationMember", "NationalMember"),
        "locations_international": get("NumberOfLocations", "LocationMember", "InternationalMember"),
        "attrition": get("TurnoverRate", "GenderMember", "PermanentEmployeesMember"),
        "employees": get("NumberOfEmployeesOrWorkersIncludingDifferentlyAbled", "GenderMember", "EmployeesMember"),
    }
    out["brsr_year_end"] = float(latest[:4])
    return {k: v for k, v in out.items() if v is not None}


def _brsr(ev: Evidence, identity: SecurityIdentity, client: NSEClient) -> None:
    sym = requests.utils.quote(identity.nse_symbol, safe="")
    url = f"{BASE}/corporate-bussiness-sustainabilitiy?index=equities&symbol={sym}"
    try:
        rows = (client.get_json(url) or {}).get("data", [])
        rows = [r for r in rows if r.get("xbrlFile")]
        if not rows:
            ev.notes["brsr"] = "No BRSR filed (mandatory only for the 1,000 largest listed companies)."
            return
        row = max(rows, key=lambda r: (r.get("fyTo") or 0, r.get("submissionDate") or ""))
        values = parse_brsr(client.get_document(row["xbrlFile"]))
    except (ProviderUnavailable, ET.ParseError, ValueError) as exc:
        ev.errors.append(f"BRSR unavailable: {exc}")
        return
    fy = f"FY{str(row.get('fyTo'))[-2:]}" if row.get("fyTo") else "latest"
    note = f"BRSR {fy}, filed with NSE {row.get('submissionDate', '')}."
    for k, v in values.items():
        ev.put(k, v, note)
    ev.sources.append(Source(title=f"Business Responsibility & Sustainability Report {fy} — {identity.nse_symbol}",
                             provider="NSE (company filing)", kind=SourceKind.COMPANY_FILING,
                             url=row["xbrlFile"], notes=note))


# ---------------------------------------------------------------------------- announcements

def summarise_announcements(rows: list[dict], today: date) -> dict[str, float]:
    def when(r) -> Optional[date]:
        try:
            return datetime.strptime(r.get("an_dt", "")[:11], "%d-%b-%Y").date()
        except ValueError:
            return None

    out = dict.fromkeys(("red_flag_events", "regulatory_actions", "auditor_resignations", "kmp_resignations",
                         "investor_comms_12m", "acquisitions_3y", "buybacks_3y", "rating_downgrades",
                         "rating_upgrades"), 0.0)
    for r in rows:
        desc, text = r.get("desc") or "", r.get("attchmntText") or ""
        both = f"{desc} {text}"
        d = when(r)
        if any(desc.startswith(x) for x in RED_FLAG_DESCS) or re.search(r"insolvency|default in (re)?payment", both, re.I):
            out["red_flag_events"] += 1
        if desc.startswith("Action(s)"):
            out["regulatory_actions"] += 1
        if re.search(r"resign", both, re.I) and re.search(r"statutory auditor|auditors? of the company", both, re.I):
            out["auditor_resignations"] += 1
        elif desc.startswith("Resignation"):
            out["kmp_resignations"] += 1
        if d and (today - d).days <= 365 and INVESTOR_COMMS.search(both):
            out["investor_comms_12m"] += 1
        if desc.startswith("Acquisition"):
            out["acquisitions_3y"] += 1
        if "buyback" in desc.lower():
            out["buybacks_3y"] += 1
        if desc.startswith("Credit Rating"):
            if re.search(r"downgrad", text, re.I):
                out["rating_downgrades"] += 1
            elif re.search(r"upgrad", text, re.I):
                out["rating_upgrades"] += 1
    return out


def _announcements(ev: Evidence, identity: SecurityIdentity, client: NSEClient, today: date) -> Optional[str]:
    sym = requests.utils.quote(identity.nse_symbol, safe="")
    start = today - timedelta(days=3 * 365)
    url = (f"{BASE}/corporate-announcements?index=equities&symbol={sym}"
           f"&from_date={start:%d-%m-%Y}&to_date={today:%d-%m-%Y}")
    try:
        rows = client.get_json(url) or []
    except ProviderUnavailable as exc:
        ev.errors.append(f"Announcements unavailable: {exc}")
        return None
    note = f"{len(rows)} NSE corporate announcements, {start:%b %Y}–{today:%b %Y}."
    for k, v in summarise_announcements(rows, today).items():
        ev.put(k, v, note)
    ev.sources.append(Source(title=f"NSE corporate announcements, last 3 years — {identity.nse_symbol}",
                             provider="NSE (company filing)", kind=SourceKind.COMPANY_FILING, url=url, notes=note))
    return next((r.get("smIndustry") for r in rows if r.get("smIndustry")), None)


# ---------------------------------------------------------------------------- shareholding

def _promoter_trend(ev: Evidence, identity: SecurityIdentity, client: NSEClient) -> None:
    sym = requests.utils.quote(identity.nse_symbol, safe="")
    try:
        rows = client.get_json(f"{BASE}/corporate-share-holdings-master?index=equities&symbol={sym}") or []
    except ProviderUnavailable as exc:
        ev.errors.append(f"Shareholding history unavailable: {exc}")
        return
    series = []
    for r in rows:
        try:
            series.append((datetime.strptime(r["date"], "%d-%b-%Y").date(), float(r["pr_and_prgrp"])))
        except (KeyError, TypeError, ValueError):
            continue
    series.sort()
    if len(series) < 2:
        return
    latest = series[-1]
    past = min(series, key=lambda x: abs((latest[0] - x[0]).days - 3 * 365))
    if (latest[0] - past[0]).days < 365:
        return
    ev.put("promoter_holding", latest[1], f"Promoter & group holding, {latest[0]:%b %Y} shareholding pattern.")
    ev.put("promoter_holding_change", latest[1] - past[1],
           f"Change in promoter holding (percentage points), {past[0]:%b %Y} to {latest[0]:%b %Y}.")


# ---------------------------------------------------------------------------- valuation history

def _fy_end_pe(ev: Evidence, identity: SecurityIdentity, facts: list[FinancialFact],
               bhav: NSEBhavcopyProvider, current_pe: Optional[float]) -> None:
    eps: dict[date, float] = {}
    for f in facts:  # as-filed EPS matches the unadjusted historical price
        if f.metric in ("diluted_eps", "diluted_eps_reported") and f.value is not None:
            if f.metric == "diluted_eps_reported" or f.period_end not in eps:
                eps[f.period_end] = f.value
    pes, used = [], []
    for end in sorted(eps)[-6:-1] if current_pe is not None else []:  # past years; the latest is "current"
        if eps[end] <= 0:
            continue
        hit = bhav.close_on_or_before(end, identity.nse_symbol, identity.isin)
        if hit:
            pes.append(hit[1] / eps[end])
            used.append(f"{end:%b %Y} {hit[1] / eps[end]:.1f}x")
    if len(pes) >= 3 and current_pe is not None:
        med = median(pes)
        ev.put("pe_vs_history", current_pe / med,
               f"Current P/E {current_pe:.1f}x vs median {med:.1f}x of financial-year-end P/Es "
               f"({', '.join(used)}); NSE bhavcopy closes and as-filed EPS.")


# ---------------------------------------------------------------------------- entry point

def collect(identity: SecurityIdentity, facts: list[FinancialFact], current_pe: Optional[float],
            client: NSEClient, bhav: NSEBhavcopyProvider, cache_dir: Path,
            today: Optional[date] = None) -> Evidence:
    today = today or datetime.now(timezone.utc).date()
    ev = Evidence()
    _sector_and_tier(ev, identity, cache_dir)
    if identity.nse_symbol:
        _brsr(ev, identity, client)
        sm_industry = _announcements(ev, identity, client, today)
        if ev.sector is None and sm_industry:
            ev.sector, ev.sector_source = sm_industry, "NSE announcement industry tag"
        _promoter_trend(ev, identity, client)
        try:
            _fy_end_pe(ev, identity, facts, bhav, current_pe)
        except Exception as exc:  # price history is optional; never break the tab
            ev.errors.append(f"Historical P/E unavailable: {exc}")
    return ev


def collect_with_registry(identity: SecurityIdentity, facts: list[FinancialFact], current_pe: Optional[float],
                          registry, cache_dir: Path) -> Evidence:
    """`collect` using the app's configured NSE providers; empty when NSE access is disabled."""
    from eii.providers.nse_filings import NSEFilingsProvider

    filings = next((p for p in registry.fundamentals if isinstance(p, NSEFilingsProvider)), None)
    bhav = next((p for p in registry.fundamentals if isinstance(p, NSEBhavcopyProvider)), None)
    if filings is None or bhav is None or not filings.enabled:
        ev = Evidence()
        ev.errors.append("NSE access is disabled (EII_ENABLE_NSE_FILINGS=false); public evidence not collected.")
        return ev
    return collect(identity, facts, current_pe, filings.client, bhav, cache_dir)

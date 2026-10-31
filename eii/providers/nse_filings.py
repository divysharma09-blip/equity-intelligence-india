"""Official company filings from NSE: annual results (XBRL), shareholding pattern, promoter pledges.

Endpoints (public JSON used by nseindia.com; verified 2026-10-27):
  /api/corporates-financial-results?index=equities&symbol=X&period=Annual   results up to Dec 2024
  /api/integrated-filing-results?index=equities&symbol=X&type=Integrated%20Filing-%20Financials
                                                                            SEBI Integrated Filing, 2025+
  /api/corporate-share-holdings-master?index=equities&symbol=X              shareholding pattern
  /api/corporate-pledgedata?index=equities&symbol=X                         promoter pledge (SAST)
The homepage and the live-quote API return 403 to scripts, so prices still come from Yahoo.

Courtesy rules: at least `min_interval` seconds between requests, listings cached for
`listing_ttl_hours`, XBRL documents (immutable once filed) cached permanently on disk.
nseindia.com terms restrict automated access; this provider makes a handful of
requests per company for personal research and can be disabled (EII_ENABLE_NSE_FILINGS=false).

Mapping policy: only filed values are used; anything combined from several filed
lines (EBIT, EBITDA, total debt, cash, shares, NII…) is labelled DERIVED. One
statement basis (consolidated preferred, else standalone) is used for every year so
that bases are never mixed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import requests

from eii.models import (AuditInfo, DataStatus, DataWarning, FinancialFact, OwnershipSnapshot,
                        SecurityIdentity, Source, SourceKind)
from eii.providers.base import (FundamentalsBundle, FundamentalsProvider, ProviderStatus,
                                ProviderUnavailable)
from eii.providers.xbrl import XbrlFiling, XbrlParseError, detect_format, parse_results_xbrl

log = logging.getLogger("eii.providers")

BASE = "https://www.nseindia.com/api"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MAX_YEARS = 8


class NSEClient:
    """Polite HTTP client: rate limit, retries with backoff, on-disk cache."""

    def __init__(self, cache_dir: Path, min_interval: float = 1.0, timeout: int = 20,
                 listing_ttl_hours: float = 12, session: Optional[requests.Session] = None):
        self.cache_dir = cache_dir
        self.min_interval = min_interval
        self.timeout = timeout
        self.listing_ttl = listing_ttl_hours * 3600
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": _UA, "Accept": "application/json, text/plain, */*",
                                     "Accept-Language": "en-US,en;q=0.9",
                                     "Referer": "https://www.nseindia.com/"})
        self._last = 0.0

    def _get(self, url: str) -> requests.Response:
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                resp = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:
                last_exc = exc
            else:
                if resp.status_code == 200:
                    return resp
                if resp.status_code in (401, 403, 404):
                    raise ProviderUnavailable(f"NSE refused the request (HTTP {resp.status_code}) for {url}")
                last_exc = RuntimeError(f"HTTP {resp.status_code}")
            time.sleep(2 ** attempt)
        raise ProviderUnavailable(f"NSE request failed after retries: {last_exc}")

    def get_json(self, url: str):
        key = hashlib.sha1(url.encode()).hexdigest()[:20]
        path = self.cache_dir / "nse_api" / f"{key}.json"
        if path.exists() and time.time() - path.stat().st_mtime < self.listing_ttl:
            return json.loads(path.read_text(encoding="utf-8"))
        try:
            text = self._get(url).text
            data = json.loads(text)
        except (ProviderUnavailable, ValueError) as exc:
            if path.exists():  # stale cache beats nothing; caller is told via log
                log.warning("Using stale NSE cache for %s (%s)", url, exc)
                return json.loads(path.read_text(encoding="utf-8"))
            if isinstance(exc, ValueError):
                raise ProviderUnavailable(f"NSE returned non-JSON for {url}") from exc
            raise
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return data

    def get_document(self, url: str) -> bytes:
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", url.rsplit("/", 1)[-1])[:150]
        path = self.cache_dir / "filings" / name
        if path.exists() and path.stat().st_size > 0:
            return path.read_bytes()
        content = self._get(url).content
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(content)
        tmp.replace(path)
        return content


# ---------------------------------------------------------------------------- listings

@dataclass
class FilingRef:
    period_end: date
    basis: str            # Consolidated / Standalone
    url: str
    broadcast: Optional[datetime]
    origin: str           # legacy / integrated


def _dt(text: Optional[str], fmts=("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y")) -> Optional[datetime]:
    if not text:
        return None
    for f in fmts:
        try:
            return datetime.strptime(text.strip().title(), f)
        except ValueError:
            continue
    return None


def _basis(text: str) -> str:
    return "Standalone" if (text or "").lower().startswith(("non", "stand")) else "Consolidated"


def annual_filing_refs(legacy: list[dict], integrated: list[dict]) -> list[FilingRef]:
    """Annual-results XBRL documents, newest revision per (period, basis)."""
    refs: list[FilingRef] = []
    for x in legacy or []:
        url = x.get("xbrl") or ""
        end = _dt(x.get("toDate"))
        if not url.endswith(".xml") or end is None:
            continue
        refs.append(FilingRef(end.date(), _basis(x.get("consolidated", "")), url,
                              _dt(x.get("broadCastDate")), "legacy"))
    fy_months = Counter(r.period_end.month for r in refs) or Counter({3: 1})
    fy_month = fy_months.most_common(1)[0][0]
    for x in integrated or []:
        url = x.get("xbrl") or ""
        end = _dt(x.get("qe_Date"))
        if not url.endswith(".xml") or end is None or end.month != fy_month:
            continue
        refs.append(FilingRef(end.date(), _basis(x.get("consolidated", "")), url,
                              _dt(x.get("broadcast_Date")), "integrated"))
    best: dict[tuple, FilingRef] = {}
    for r in refs:
        k = (r.period_end, r.basis)
        cur = best.get(k)
        newer = (r.origin == "integrated" and (cur is None or cur.origin == "legacy")) or (
            cur is not None and cur.origin == r.origin and (r.broadcast or datetime.min) > (cur.broadcast or datetime.min))
        if cur is None or newer:
            best[k] = r
    return sorted(best.values(), key=lambda r: (r.period_end, r.basis), reverse=True)


def choose_basis(refs: list[FilingRef], prefer: str = "Consolidated") -> Optional[str]:
    """Preferred basis if the latest year has it, else the other. Applied to every year.
    Banks prefer Standalone: asset-quality and capital ratios are reported there (consolidated
    bank filings tag them as 0.00)."""
    if not refs:
        return None
    latest = max(r.period_end for r in refs)
    bases = {r.basis for r in refs if r.period_end == latest}
    return prefer if prefer in bases else next(iter(bases))


# ---------------------------------------------------------------------------- mapping

FACTS = list[tuple[str, float, DataStatus, str]]  # metric, value, status, unit


def _first(d: dict[str, float], *names: str) -> Optional[float]:
    return next((d[n] for n in names if n in d), None)


def _first_nonzero(d: dict[str, float], *names: str) -> Optional[float]:
    """Headline items: some filings leave the preferred element as 0.00 and fill an
    alternative one. Prefer a non-zero candidate; return 0 only if every candidate is 0."""
    vals = [d[n] for n in names if n in d]
    return next((v for v in vals if v != 0), vals[0] if vals else None)


def _sum(d: dict[str, float], *names: str) -> Optional[float]:
    vals = [d[n] for n in names if n in d]
    return sum(vals) if vals else None


def _common(f: XbrlFiling, out: FACTS, ccy: str) -> None:
    a = f.annual

    def put(metric, value, status=DataStatus.REPORTED, unit=ccy):
        if value is not None:
            out.append((metric, float(value), status, unit))

    put("pretax_income", _first(a, "ProfitBeforeTax", "ProfitLossFromOrdinaryActivitiesBeforeTax"))
    put("tax_provision", _first(a, "TaxExpense"))
    ni = _first_nonzero(a, "ProfitOrLossAttributableToOwnersOfParent",
                        "ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates",
                        "ProfitLossForPeriod", "ProfitLossForThePeriod")
    put("net_income", ni)
    eps = _first_nonzero(a, "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                         "DilutedEarningsLossPerShareFromContinuingOperations",
                         "DilutedEarningsPerShareAfterExtraordinaryItems",
                         "DilutedEarningsPerShareBeforeExtraordinaryItems")
    if eps == 0 and ni:
        eps = None  # zero EPS with non-zero profit is an untagged field, not a fact
    put("diluted_eps", eps, unit=f"{ccy}/share")
    put("other_income", _first(a, "OtherIncome"))
    put("operating_cash_flow", _first(a, "CashFlowsFromUsedInOperatingActivities"))
    capex = _sum(a, "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
                 "PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities",
                 "PurchaseOfTangibleAssetsClassifiedAsInvestingActivities")
    put("capex", -abs(capex) if capex is not None else None, DataStatus.DERIVED)  # outflow, negative
    div = _first(a, "DividendsPaidClassifiedAsFinancingActivities")
    put("dividends_paid", -abs(div) if div is not None else None)
    paid_up, fv = _first(a, "PaidUpValueOfEquityShareCapital"), _first(a, "FaceValueOfEquityShareCapital")
    if paid_up and fv:
        put("shares_outstanding", paid_up / fv, DataStatus.DERIVED, "shares")


def map_industrial(f: XbrlFiling, ccy: str) -> FACTS:
    out: FACTS = []
    a, i = f.annual, f.instant
    _common(f, out, ccy)

    def put(metric, value, status=DataStatus.REPORTED):
        if value is not None:
            out.append((metric, float(value), status, ccy))

    put("revenue", _first_nonzero(a, "RevenueFromOperations"))
    fin = _first(a, "FinanceCosts")
    put("interest_expense", fin)
    dep = _first(a, "DepreciationDepletionAndAmortisationExpense")
    put("depreciation", dep)
    pbe = _first(a, "ProfitBeforeExceptionalItemsAndTax", "ProfitBeforeTax")
    if pbe is not None and fin is not None:
        ebit = pbe + fin
        put("ebit", ebit, DataStatus.DERIVED)
        if dep is not None:
            put("ebitda", ebit + dep, DataStatus.DERIVED)
    put("total_assets", _first(i, "Assets"))
    put("total_equity", _first(i, "EquityAttributableToOwnersOfParent", "Equity"))
    put("total_debt", _sum(i, "BorrowingsNoncurrent", "BorrowingsCurrent"), DataStatus.DERIVED)
    put("cash_and_st_investments", _sum(i, "CashAndCashEquivalents", "BankBalanceOtherThanCashAndCashEquivalents",
                                        "CurrentInvestments"), DataStatus.DERIVED)
    put("current_assets", _first(i, "CurrentAssets"))
    put("current_liabilities", _first(i, "CurrentLiabilities"))
    put("receivables", _first(i, "TradeReceivablesCurrent"))
    put("inventory", _first(i, "Inventories"))
    put("short_term_debt", _first(i, "BorrowingsCurrent"))
    put("raw_material_cost", _sum(a, "CostOfMaterialsConsumed", "PurchasesOfStockInTrade",
                                  "ChangesInInventoriesOfFinishedGoodsWorkInProgressAndStockInTrade"), DataStatus.DERIVED)
    put("employee_cost", _first(a, "EmployeeBenefitExpense"))
    return out


def map_bank(f: XbrlFiling, ccy: str) -> FACTS:
    out: FACTS = []
    a, i = f.annual, f.instant
    _common(f, out, ccy)

    def put(metric, value, status=DataStatus.REPORTED, unit=ccy):
        if value is not None:
            out.append((metric, float(value), status, unit))

    ie, ix = _first(a, "InterestEarned"), _first(a, "InterestExpended")
    put("interest_earned", ie)
    put("interest_expended", ix)
    if ie is not None and ix is not None:
        put("net_interest_income", ie - ix, DataStatus.DERIVED)
    put("revenue", _first(a, "Income"))
    put("operating_expenses", _first(a, "OperatingExpenses"))
    put("pre_provision_profit", _first(a, "OperatingProfitBeforeProvisionAndContingencies"))
    put("provisions", _first(a, "ProvisionsOtherThanTaxAndContingencies"))
    put("gross_npa", _first(a, "GrossNonPerformingAssets"))
    put("net_npa", _first(a, "NonPerformingAssets"))
    # A bank cannot have 0% GNPA, ROA or CET1: zeros here are untagged fields, not facts.
    gnpa = _first(a, "PercentageOfGrossNpa") or None
    put("gnpa_pct", gnpa, unit="ratio")
    nnpa = _first(a, "PercentageOfNpa")
    put("nnpa_pct", nnpa if gnpa is not None else None, unit="ratio")
    put("roa_reported", _first(a, "ReturnOnAssets") or None, unit="ratio")
    put("cet1_ratio", _first(a, "CET1Ratio") or None, unit="ratio")
    put("total_assets", _first(i, "Assets"))
    cap, res = _first(i, "Capital"), _first(i, "ReservesAndSurplus")
    if cap is not None and res is not None:
        put("total_equity", cap + res, DataStatus.DERIVED)
    put("deposits", _first(i, "Deposits"))
    put("advances", _first(i, "Advances"))
    put("total_debt", _first(i, "Borrowings"))
    return out


def map_nbfc(f: XbrlFiling, ccy: str) -> FACTS:
    out: FACTS = []
    a, i = f.annual, f.instant
    _common(f, out, ccy)

    def put(metric, value, status=DataStatus.REPORTED):
        if value is not None:
            out.append((metric, float(value), status, ccy))

    ie, fc = _first(a, "InterestEarned", "InterestIncome"), _first(a, "FinanceCosts")
    put("interest_earned", ie)
    put("interest_expended", fc)
    if ie is not None and fc is not None:
        put("net_interest_income", ie - fc, DataStatus.DERIVED)
    put("revenue", _first(a, "RevenueFromOperations"))
    put("provisions", _first(a, "ImpairmentOnFinancialInstruments"))
    put("depreciation", _first(a, "DepreciationDepletionAndAmortisationExpense"))
    put("total_assets", _first(i, "Assets"))
    put("total_equity", _first(i, "EquityAttributableToOwnersOfParent", "Equity"))
    put("advances", _first(i, "Loans"))
    put("deposits", _first(i, "Deposits"))
    put("total_debt", _sum(i, "DebtSecurities", "Borrowings", "Deposits", "SubordinatedLiabilities"),
        DataStatus.DERIVED)
    return out


MAPPERS: dict[str, Callable[[XbrlFiling, str], FACTS]] = {
    "industrial": map_industrial, "bank": map_bank, "nbfc": map_nbfc}


def sanity_check(f: XbrlFiling, facts: FACTS) -> tuple[FACTS, list[str]]:
    """Internal-consistency checks that expose scaling or tagging errors in a filing.
    If net profit and EPS × shares disagree, we cannot tell which is wrong, so BOTH are dropped."""
    out = []
    v = {m: val for m, val, _, _ in facts}
    eps, shares, ni = v.get("diluted_eps"), v.get("shares_outstanding"), v.get("net_income")
    if eps and shares and ni and ni > 0 and eps > 0:
        implied = eps * shares
        if not 0.8 <= implied / ni <= 1.25:
            facts = [x for x in facts if x[0] not in ("net_income", "diluted_eps")]
            out.append(f"FY{f.period_end.year} filing is internally inconsistent: EPS × shares "
                       f"(₹{implied / 1e7:,.0f} Cr) vs net profit (₹{ni / 1e7:,.0f} Cr). Both figures "
                       "are excluded for that year.")
    assets, eq_liab = f.instant.get("Assets"), f.instant.get("EquityAndLiabilities") or f.instant.get(
        "CapitalAndLiabilities")
    if assets and eq_liab and abs(assets - eq_liab) / assets > 0.005:
        out.append(f"FY{f.period_end.year}: balance sheet does not balance in the filing "
                   f"(assets {assets:,.0f} vs equity + liabilities {eq_liab:,.0f}).")
    return facts, out


# ---------------------------------------------------------------------------- ownership

def _f(x) -> Optional[float]:
    try:
        return float(str(x).strip())
    except (TypeError, ValueError):
        return None


def parse_ownership(shp: list[dict], pledge: dict) -> tuple[Optional[OwnershipSnapshot], list[str], Optional[str]]:
    """Returns (snapshot, warnings, shareholding XBRL url of the latest quarter)."""
    warnings: list[str] = []
    rows = []
    for x in shp or []:
        d = _dt(x.get("date"))
        if d is not None:
            rows.append((d.date(), _f(x.get("pr_and_prgrp")), _f(x.get("public_val")), x.get("xbrl"),
                         _dt(x.get("broadcastDate")) or datetime.min))
    if not rows:
        return None, ["No shareholding pattern found on NSE."], None
    latest_by_date: dict[date, tuple] = {}
    for r in rows:  # keep the most recently broadcast (revised) filing per quarter
        if r[0] not in latest_by_date or r[4] > latest_by_date[r[0]][4]:
            latest_by_date[r[0]] = r
    ordered = sorted(latest_by_date.values(), key=lambda r: r[0], reverse=True)
    as_of, promoter, public, url, _ = ordered[0]
    snap = OwnershipSnapshot(as_of=as_of, promoter_holding_pct=promoter, public_holding_pct=public,
                             promoter_history=[(r[0], r[1]) for r in ordered[:12] if r[1] is not None])
    p = (pledge or {}).get("data") or []
    if p:
        rec = p[0]
        pledge_date = _dt(rec.get("shp"))
        promo_shares, promo_pledged = _f(rec.get("totPromoterHolding")), _f(rec.get("totPromoterShares"))
        total = _f(rec.get("totIssuedShares"))
        pct_of_promoter, pct_of_total = _f(rec.get("percPromoterShares")), _f(rec.get("percTotShares"))
        # Verify the reported percentages against the share counts before using them.
        ok = True
        if promo_shares and promo_pledged is not None and pct_of_promoter is not None:
            ok &= abs(promo_pledged / promo_shares * 100 - pct_of_promoter) < 0.1
        if total and promo_pledged is not None and pct_of_total is not None:
            ok &= abs(promo_pledged / total * 100 - pct_of_total) < 0.1
        if not promo_shares or not promoter:
            warnings.append("Company has no promoter holding; promoter pledge is not applicable.")
        elif ok:
            snap.promoter_pledged_pct_of_promoter = pct_of_promoter
            snap.promoter_pledged_pct_of_total = pct_of_total
        else:
            warnings.append("NSE pledge percentages are inconsistent with the share counts; pledge data not used.")
        if pledge_date and pledge_date.date() != as_of:
            warnings.append(f"Pledge data is as of {pledge_date:%d %b %Y}; shareholding as of {as_of:%d %b %Y}.")
    else:
        warnings.append("No promoter pledge disclosure found on NSE.")
    return snap, warnings, url


# ---------------------------------------------------------------------------- corporate actions

_BONUS = re.compile(r"bonus\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)", re.I)
_SPLIT = re.compile(r"from\s*(?:rs|re|inr)?\.?\s*(\d+(?:\.\d+)?)\s*/?-?.*?\bto\s*(?:rs|re|inr)?\.?\s*"
                    r"(\d+(?:\.\d+)?)", re.I)


def share_multiplier(subject: str) -> Optional[float]:
    """How many shares one pre-action share becomes, from an NSE corporate-action subject.
    'Bonus 4:1' -> 5; 'Face Value Split From Rs 2 To Re 1' -> 2; combined subjects multiply.
    Returns None for actions that do not change the share count (dividends, AGMs…)."""
    factor, matched = 1.0, False
    subj = subject or ""
    for m in _BONUS.finditer(subj):
        new, held = float(m.group(1)), float(m.group(2))
        if held > 0:
            factor *= (new + held) / held
            matched = True
    if re.search(r"split|sub-?division", subj, re.I):
        m = _SPLIT.search(subj)
        if m and float(m.group(2)) > 0:
            factor *= float(m.group(1)) / float(m.group(2))
            matched = True
    return factor if matched and factor > 1 else None


@dataclass
class ShareAction:
    ex_date: date
    multiplier: float
    subject: str


def parse_corporate_actions(rows: list[dict]) -> list[ShareAction]:
    seen, out = set(), []
    for x in rows or []:
        ex = _dt(x.get("exDate"))
        mult = share_multiplier(x.get("subject", ""))
        if ex is None or mult is None:
            continue
        key = (ex.date(), round(mult, 6))
        if key not in seen:   # NSE sometimes lists the same action twice (bonus and split rows)
            seen.add(key)
            out.append(ShareAction(ex.date(), mult, x.get("subject", "").strip()))
    # Bonus and split on the same ex-date appear as separate rows; combined subjects as one.
    return sorted(out, key=lambda a: a.ex_date)


def adjust_per_share(facts: list[FinancialFact], actions: list[ShareAction]) -> tuple[list[FinancialFact], list[str]]:
    """Restate EPS and share counts to today's share basis for splits/bonuses after each period end.
    The as-filed EPS is kept as `diluted_eps_reported`."""
    if not actions:
        return facts, []
    out, notes = [], []
    for f in facts:
        later = [a for a in actions if a.ex_date > f.period_end]
        factor = 1.0
        for a in later:
            factor *= a.multiplier
        if factor == 1.0 or f.metric not in ("diluted_eps", "shares_outstanding") or f.value is None:
            out.append(f)
            continue
        if f.metric == "diluted_eps":
            out.append(f.model_copy(update={"metric": "diluted_eps_reported"}))
            out.append(f.model_copy(update={"value": f.value / factor, "status": DataStatus.DERIVED}))
        else:
            out.append(f.model_copy(update={"value": f.value * factor, "status": DataStatus.DERIVED}))
    for a in actions:
        notes.append(f"{a.ex_date:%d %b %Y}: {a.subject} (×{a.multiplier:g} shares)")
    return out, notes


# ---------------------------------------------------------------------------- provider

class NSEFilingsProvider(FundamentalsProvider):
    name = "NSE corporate filings (official XBRL)"
    kind = SourceKind.COMPANY_FILING
    description = ("Annual results (P&L, balance sheet, cash flow, bank asset quality), shareholding "
                   "pattern and promoter pledges, as filed by the company with NSE.")

    def __init__(self, client: NSEClient, enabled: bool = True):
        self.client = client
        self.enabled = enabled

    def status(self) -> ProviderStatus:
        if not self.enabled:
            return ProviderStatus(self.name, False, "Disabled (EII_ENABLE_NSE_FILINGS=false)")
        return ProviderStatus(self.name, True, "Enabled. Rate-limited; filings cached on disk.")

    def fetch(self, identity: SecurityIdentity) -> FundamentalsBundle:
        if not self.enabled:
            raise ProviderUnavailable(self.status().message)
        if not identity.nse_symbol:
            raise ProviderUnavailable("NSE filings need an NSE symbol (company not listed on NSE).")
        sym = requests.utils.quote(identity.nse_symbol, safe="")
        bundle = FundamentalsBundle()
        now = datetime.now(timezone.utc)

        legacy_url = f"{BASE}/corporates-financial-results?index=equities&symbol={sym}&period=Annual"
        integ_url = (f"{BASE}/integrated-filing-results?index=equities&symbol={sym}"
                     "&type=Integrated%20Filing-%20Financials")
        legacy = self._safe_json(legacy_url, bundle, "annual results (2019–2024)") or []
        integrated = (self._safe_json(integ_url, bundle, "integrated filings (2025+)") or {}).get("data", [])
        self._statements(identity, legacy, integrated, bundle, now)
        if bundle.facts:
            ca_url = f"{BASE}/corporates-corporateActions?index=equities&symbol={sym}"
            ca = self._safe_json(ca_url, bundle, "corporate actions")
            if ca is None:
                bundle.warnings.append(DataWarning(
                    severity="warning", section="Financial statements",
                    message="Corporate actions unavailable: historical EPS is NOT adjusted for splits/bonuses."))
            else:
                actions = [a for a in parse_corporate_actions(ca)
                           if a.ex_date > min(f.period_end for f in bundle.facts)]
                bundle.facts, notes = adjust_per_share(bundle.facts, actions)
                if notes:
                    src = Source(title=f"NSE corporate actions — {identity.nse_symbol}",
                                 provider="NSE (company filing)", kind=SourceKind.COMPANY_FILING,
                                 url=ca_url, retrieved_at=now,
                                 notes="Splits/bonuses used to restate per-share figures.")
                    bundle.sources.append(src)
                    bundle.warnings.append(DataWarning(
                        severity="info", section="Financial statements",
                        message="EPS and share counts restated to today's share basis (as-filed EPS kept as "
                                "'diluted_eps_reported'). Actions applied: " + "; ".join(notes)))

        shp_url = f"{BASE}/corporate-share-holdings-master?index=equities&symbol={sym}"
        pledge_url = f"{BASE}/corporate-pledgedata?index=equities&symbol={sym}"
        shp = self._safe_json(shp_url, bundle, "shareholding pattern")
        pledge = self._safe_json(pledge_url, bundle, "promoter pledge data")
        if shp is not None:
            snap, warns, shp_doc = parse_ownership(shp, pledge or {})
            for w in warns:
                bundle.warnings.append(DataWarning(severity="info", section="Governance", message=w))
            if snap:
                src = Source(title=f"NSE shareholding pattern — {identity.nse_symbol}, quarter ended {snap.as_of:%d %b %Y}",
                             provider="NSE (company filing)", kind=SourceKind.COMPANY_FILING,
                             url=shp_doc if shp_doc and shp_doc.endswith(".xml") else shp_url, retrieved_at=now,
                             notes="Promoter & public holding as filed (Reg. 31 SEBI LODR). Pledge data from "
                                   f"NSE SAST pledge disclosures: {pledge_url}")
                bundle.sources.append(src)
                snap.source_id = src.source_id
                bundle.ownership = snap

        if not (bundle.facts or bundle.ownership):
            raise ProviderUnavailable("NSE returned no usable filings for this company. " +
                                      " ".join(w.message for w in bundle.warnings))
        return bundle

    def _safe_json(self, url: str, bundle: FundamentalsBundle, label: str):
        try:
            return self.client.get_json(url)
        except ProviderUnavailable as exc:
            bundle.warnings.append(DataWarning(severity="warning", section="Data providers",
                                               message=f"NSE {label} unavailable: {exc}"))
            return None

    def _statements(self, identity: SecurityIdentity, legacy: list, integrated: list,
                    bundle: FundamentalsBundle, now: datetime) -> None:
        refs = annual_filing_refs(legacy, integrated)
        is_bank = any(detect_format(r.url) == "bank" for r in refs)
        basis = choose_basis(refs, prefer="Standalone" if is_bank else "Consolidated")
        if basis is None:
            bundle.warnings.append(DataWarning(severity="warning", section="Financial statements",
                                               message="No annual results XBRL filings found on NSE."))
            return
        chosen = [r for r in refs if r.basis == basis][:MAX_YEARS]
        skipped_years = sorted({r.period_end.year for r in refs} - {r.period_end.year for r in chosen})
        if skipped_years:
            bundle.warnings.append(DataWarning(
                severity="info", section="Financial statements",
                message=f"Years {skipped_years} have no {basis.lower()} filing and are omitted "
                        "(bases are never mixed)."))
        bundle.statement_basis = basis
        latest_filing: Optional[tuple[XbrlFiling, Source]] = None
        legacy_years: list[int] = []
        for ref in chosen:
            try:
                filing = parse_results_xbrl(self.client.get_document(ref.url), ref.url)
            except (ProviderUnavailable, XbrlParseError) as exc:
                bundle.warnings.append(DataWarning(severity="warning", section="Financial statements",
                                                   message=f"Filing for FY ending {ref.period_end:%b %Y} "
                                                           f"could not be read: {exc}"))
                continue
            if not filing.has_annual_context:
                bundle.warnings.append(DataWarning(severity="warning", section="Financial statements",
                                                   message=f"Filing {ref.url.rsplit('/', 1)[-1]} has no "
                                                           "full-year figures; skipped."))
                continue
            # The latest filing decides the company type; older years are mapped the same way even if
            # they used a generic taxonomy (e.g. NBFCs filed under plain Ind-AS before 2025).
            bundle.company_type = bundle.company_type or (filing.format if filing.format != "unknown" else None)
            mapper = MAPPERS.get(bundle.company_type or filing.format)
            if mapper is None:
                bundle.warnings.append(DataWarning(
                    severity="warning", section="Financial statements",
                    message=f"Filing format '{filing.format}' (e.g. insurer) is not mapped yet; "
                            "statements from this filing are not used."))
                continue
            ccy = (filing.currency or "INR").upper()
            fy = filing.period_end.year
            src = Source(
                title=f"NSE annual results FY{fy} ({filing.basis or basis}) — XBRL filing",
                provider="NSE (company filing)", kind=SourceKind.COMPANY_FILING, url=ref.url,
                retrieved_at=now,
                notes=(f"{ref.origin} format; broadcast {ref.broadcast:%d %b %Y %H:%M}" if ref.broadcast
                       else f"{ref.origin} format") + ". Point-in-time: available from broadcast date.")
            facts, sanity_notes = sanity_check(filing, mapper(filing, ccy))
            if not facts:
                continue
            bundle.sources.append(src)
            for metric, value, status, unit in facts:
                bundle.facts.append(FinancialFact(metric=metric, period_end=filing.period_end, value=value,
                                                  unit=unit, status=status, source_id=src.source_id))
            for note in filing.rejected:
                if note.startswith("Context periods"):
                    legacy_years.append(fy)
                else:
                    bundle.warnings.append(DataWarning(severity="warning", section="Financial statements",
                                                       message=f"FY{fy} filing: {note}"))
            for note in sanity_notes:
                bundle.warnings.append(DataWarning(severity="warning", section="Financial statements",
                                                   message=note))
            if latest_filing is None or filing.period_end > latest_filing[0].period_end:
                latest_filing = (filing, src)
        if legacy_years:
            bundle.warnings.append(DataWarning(
                severity="info", section="Financial statements",
                message=f"Filings for FY{', FY'.join(map(str, sorted(legacy_years)))} have missing or mis-dated "
                        "XBRL contexts; NSE's legacy conventions were used (FourD = full year, OneI = year end). "
                        "Figures cross-checked where a second source exists."))
        if latest_filing:
            f, src = latest_filing
            opinion = f.text.get("DeclarationOfUnmodifiedOpinionOrStatementOnImpactOfAuditQualification")
            auditors = list(dict.fromkeys(a.strip() for a in f.lists.get("AuditorsFirmName", []) if a.strip()))
            if opinion or auditors:
                bundle.audit = AuditInfo(period_end=f.period_end, auditors=auditors, opinion=opinion,
                                         source_id=src.source_id)

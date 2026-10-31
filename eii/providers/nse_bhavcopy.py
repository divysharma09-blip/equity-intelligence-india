"""Official end-of-day closing prices from the NSE bhavcopy.

NSE publishes one file per trading day with every security's OHLC and close:
  https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip
(UDiFF format, verified 2026-10-29). The archive host serves plain downloads without the
cookie handshake that blocks the live-quote API, so it also works from cloud hosts where
Yahoo Finance is rate-limited.

This provider supplies only a market snapshot (latest close). It is registered after Yahoo,
so it is used when Yahoo returns no quote. Each day's file is immutable once published and is
cached on disk; a missing file (weekend, holiday, or today's not yet published) means the
previous trading day is tried.
"""

from __future__ import annotations

import io
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

from eii.models import MarketSnapshot, SecurityIdentity, Source, SourceKind
from eii.providers.base import (FundamentalsBundle, FundamentalsProvider, ProviderStatus,
                                ProviderUnavailable)

log = logging.getLogger("eii.providers")

URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
# Format used until NSE switched to UDiFF on 8 Jul 2024; still served for historical dates.
LEGACY_URL = "https://nsearchives.nseindia.com/content/historical/EQUITIES/{d:%Y}/{mon}/cm{d:%d}{mon}{d:%Y}bhav.csv.zip"
UDIFF_FROM = date(2024, 7, 8)
_LEGACY_COLUMNS = {"TIMESTAMP": "TradDt", "ISIN": "ISIN", "SYMBOL": "TckrSymb", "SERIES": "SctySrs", "CLOSE": "ClsPric"}
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_IST = timezone(timedelta(hours=5, minutes=30))
LOOKBACK_DAYS = 7
SERIES_PREFERENCE = ["EQ", "BE", "BZ", "SM", "ST"]
_COLUMNS = ["TradDt", "ISIN", "TckrSymb", "SctySrs", "ClsPric"]


def url_for(d: date) -> str:
    return URL.format(d=d) if d >= UDIFF_FROM else LEGACY_URL.format(d=d, mon=d.strftime("%b").upper())


def parse_bhavcopy(raw: bytes) -> pd.DataFrame:
    """Zipped UDiFF or legacy CSV -> frame with columns TradDt, ISIN, TckrSymb, SctySrs, ClsPric."""
    df = pd.read_csv(io.BytesIO(raw), compression="zip", dtype=str)
    if "SYMBOL" in df.columns:
        df = df.rename(columns=_LEGACY_COLUMNS)
    df = df[_COLUMNS].copy()
    df["ClsPric"] = pd.to_numeric(df["ClsPric"], errors="coerce")
    for col in ("ISIN", "TckrSymb", "SctySrs"):
        df[col] = df[col].str.strip().str.upper()
    return df


def pick_row(df: pd.DataFrame, symbol: Optional[str], isin: Optional[str]) -> Optional[pd.Series]:
    """Match on symbol (and ISIN when known); prefer the main equity series."""
    rows = df[df["TckrSymb"] == symbol] if symbol else df.iloc[0:0]
    if isin:
        by_isin = rows[rows["ISIN"] == isin]
        rows = by_isin if not by_isin.empty else df[df["ISIN"] == isin]
    rows = rows[rows["ClsPric"].notna() & (rows["ClsPric"] > 0)]
    if rows.empty:
        return None
    rank = rows["SctySrs"].map(lambda s: SERIES_PREFERENCE.index(s) if s in SERIES_PREFERENCE else 99)
    return rows.loc[rank.idxmin()]


class NSEBhavcopyProvider(FundamentalsProvider):
    name = "NSE bhavcopy (official end-of-day prices)"
    kind = SourceKind.OFFICIAL_EXCHANGE
    description = "Latest official closing price for every NSE security, published daily by NSE."

    def __init__(self, cache_dir: Path, timeout: int = 20, enabled: bool = True,
                 session: Optional[requests.Session] = None):
        self.cache_dir = cache_dir / "bhavcopy"
        self.timeout = timeout
        self.enabled = enabled
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": _UA})
        self._frames: dict[date, pd.DataFrame] = {}

    def status(self) -> ProviderStatus:
        if not self.enabled:
            return ProviderStatus(self.name, False, "Disabled (EII_ENABLE_NSE_FILINGS=false)")
        return ProviderStatus(self.name, True, "Enabled. Closing prices cached per trading day.")

    def _day(self, d: date) -> Optional[pd.DataFrame]:
        if d in self._frames:
            return self._frames[d]
        path = self.cache_dir / f"{d:%Y%m%d}.csv.zip"
        if path.exists() and path.stat().st_size > 0:
            raw = path.read_bytes()
        else:
            try:
                resp = self.session.get(url_for(d), timeout=self.timeout)
            except requests.RequestException as exc:
                log.warning("Bhavcopy download failed for %s: %s", d, exc)
                return None
            if resp.status_code != 200:
                return None  # weekend, holiday, or not yet published
            raw = resp.content
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part")
            tmp.write_bytes(raw)
            tmp.replace(path)
        try:
            frame = parse_bhavcopy(raw)
        except (ValueError, KeyError) as exc:
            log.warning("Bhavcopy for %s could not be parsed: %s", d, exc)
            return None
        self._frames[d] = frame
        return frame

    def close_on_or_before(self, d: date, symbol: Optional[str], isin: Optional[str],
                           lookback_days: int = LOOKBACK_DAYS) -> Optional[tuple[date, float]]:
        """Closing price on the last trading day on or before `d`, or None."""
        for back in range(lookback_days):
            day = d - timedelta(days=back)
            if day.weekday() >= 5:
                continue
            frame = self._day(day)
            if frame is not None:
                row = pick_row(frame, symbol, isin)
                return (day, float(row["ClsPric"])) if row is not None else None
        return None

    def latest(self) -> tuple[date, pd.DataFrame]:
        today = datetime.now(_IST).date()
        for back in range(LOOKBACK_DAYS):
            d = today - timedelta(days=back)
            if d.weekday() >= 5:
                continue
            frame = self._day(d)
            if frame is not None:
                return d, frame
        raise ProviderUnavailable(f"No NSE bhavcopy found for the last {LOOKBACK_DAYS} days.")

    def fetch(self, identity: SecurityIdentity) -> FundamentalsBundle:
        if not self.enabled:
            raise ProviderUnavailable(self.status().message)
        if not (identity.nse_symbol or identity.isin):
            raise ProviderUnavailable("NSE bhavcopy needs an NSE symbol or ISIN.")
        d, frame = self.latest()
        row = pick_row(frame, identity.nse_symbol, identity.isin)
        if row is None:
            raise ProviderUnavailable(f"{identity.nse_symbol or identity.isin} did not trade on NSE on {d:%d %b %Y}.")
        src = Source(title=f"NSE bhavcopy — closing price {row['TckrSymb']} ({row['SctySrs']}), {d:%d %b %Y}",
                     provider=self.name, kind=self.kind, url=url_for(d),
                     notes="Official end-of-day close; not a live price.")
        as_of = datetime.combine(d, datetime.min.time(), tzinfo=_IST).replace(hour=15, minute=30)
        bundle = FundamentalsBundle(sources=[src])
        bundle.market = MarketSnapshot(price=float(row["ClsPric"]), currency="INR", as_of=as_of,
                                       source_id=src.source_id)
        return bundle

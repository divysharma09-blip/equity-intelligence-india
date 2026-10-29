"""Exchange security masters.

NSE: the official equity list (EQUITY_L.csv) published by NSE, downloaded and
cached locally. If the download fails, a previously cached copy is used and its
age is reported.

BSE: BSE blocks automated access to its scrip list, so this provider reads a
CSV that the user downloads manually from bseindia.com (Markets -> Equity ->
List of Scrips -> Download). Without that file, BSE codes are reported as
unavailable rather than guessed.
"""

from __future__ import annotations

import io
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

from eii.models import Source, SourceKind
from eii.providers.base import ProviderStatus, ProviderUnavailable, SecurityMasterProvider

log = logging.getLogger("eii.providers")

_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

MASTER_COLUMNS = ["company_name", "symbol", "isin", "series", "listing_date", "face_value"]


def _clean_frame(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for col in ("company_name", "symbol", "isin", "series"):
        df[col] = df[col].astype("string").str.strip()
    df["symbol"] = df["symbol"].str.upper()
    df["isin"] = df["isin"].str.upper()
    df = df[df["symbol"].notna() & (df["symbol"] != "")]
    return df[MASTER_COLUMNS].reset_index(drop=True)


class NSEEquityMaster(SecurityMasterProvider):
    name = "NSE equity list (official)"
    exchange = "NSE"
    kind = SourceKind.OFFICIAL_EXCHANGE
    description = "Official list of equity securities listed on NSE (symbol, name, ISIN, series)."

    def __init__(self, url: str, cache_dir: Path, cache_hours: int = 24, timeout: int = 20):
        self.url = url
        self.cache_path = cache_dir / "nse_equity_list.csv"
        self.cache_hours = cache_hours
        self.timeout = timeout
        self._loaded_from: str = ""
        self._loaded_at: Optional[datetime] = None
        self.last_warning: Optional[str] = None

    def status(self) -> ProviderStatus:
        if self.cache_path.exists():
            age_h = (time.time() - self.cache_path.stat().st_mtime) / 3600
            return ProviderStatus(self.name, True, f"Cached copy is {age_h:.1f} h old")
        return ProviderStatus(self.name, True, "Not yet downloaded; will download on first search")

    def _cache_fresh(self) -> bool:
        return (self.cache_path.exists()
                and (time.time() - self.cache_path.stat().st_mtime) < self.cache_hours * 3600)

    def refresh(self) -> None:
        """Download a fresh copy. Raises ProviderUnavailable with a readable message."""
        try:
            resp = requests.get(self.url, headers={"User-Agent": _BROWSER_UA}, timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise ProviderUnavailable(f"Could not download NSE equity list: {exc}") from exc
        text = resp.text
        if "SYMBOL" not in text[:200].upper() or "ISIN" not in text[:300].upper():
            raise ProviderUnavailable("NSE returned an unexpected response (not the equity list CSV).")
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.cache_path)
        log.info("Downloaded NSE equity list (%d bytes)", len(text))

    def load(self) -> pd.DataFrame:
        self.last_warning = None
        if not self._cache_fresh():
            try:
                self.refresh()
            except ProviderUnavailable as exc:
                if not self.cache_path.exists():
                    raise
                age_h = (time.time() - self.cache_path.stat().st_mtime) / 3600
                self.last_warning = f"{exc} Using cached copy that is {age_h:.0f} h old."
                log.warning(self.last_warning)
        df = self.parse(self.cache_path.read_text(encoding="utf-8"))
        self._loaded_at = datetime.fromtimestamp(self.cache_path.stat().st_mtime, tz=timezone.utc)
        return df

    @staticmethod
    def parse(csv_text: str) -> pd.DataFrame:
        raw = pd.read_csv(io.StringIO(csv_text), dtype=str)
        raw.columns = [c.strip().upper() for c in raw.columns]
        required = {"SYMBOL", "NAME OF COMPANY", "ISIN NUMBER"}
        if not required.issubset(raw.columns):
            raise ProviderUnavailable(f"NSE equity list is missing columns: {required - set(raw.columns)}")
        df = pd.DataFrame({
            "company_name": raw["NAME OF COMPANY"],
            "symbol": raw["SYMBOL"],
            "isin": raw["ISIN NUMBER"],
            "series": raw.get("SERIES"),
            "listing_date": pd.to_datetime(raw.get("DATE OF LISTING"), format="%d-%b-%Y",
                                           errors="coerce").dt.date,
            "face_value": pd.to_numeric(raw.get("FACE VALUE"), errors="coerce"),
        })
        return _clean_frame(df)

    def source(self) -> Source:
        return Source(
            title="NSE — List of equity securities (EQUITY_L.csv)",
            provider="National Stock Exchange of India",
            kind=SourceKind.OFFICIAL_EXCHANGE,
            url=self.url,
            retrieved_at=self._loaded_at or datetime.now(timezone.utc),
            notes="Used for symbol/ISIN search and identity verification.",
        )


class BSEScripMaster(SecurityMasterProvider):
    name = "BSE scrip list (user-downloaded)"
    exchange = "BSE"
    kind = SourceKind.OFFICIAL_EXCHANGE
    description = ("BSE list of scrips, read from a CSV you download from bseindia.com. "
                   "Needed to map BSE codes to ISINs.")
    download_page = "https://www.bseindia.com/corporates/List_Scrips.html"

    # Accept the column spellings seen in BSE downloads.
    _ALIASES = {
        "symbol": ["security code", "scrip code", "scrip_cd", "code"],
        "company_name": ["issuer name", "security name", "scrip name", "company name", "name"],
        "isin": ["isin no", "isin number", "isin", "isin_number"],
        "series": ["group", "scrip group"],
        "status": ["status"],
        "face_value": ["face value", "facevalue"],
    }

    def __init__(self, cache_dir: Path):
        self.path = cache_dir / "bse_equity_list.csv"

    def status(self) -> ProviderStatus:
        if self.path.exists():
            return ProviderStatus(self.name, True, f"Using {self.path.name}")
        return ProviderStatus(self.name, False,
                              f"Not configured. Download the list of scrips from {self.download_page} "
                              f"and save it as {self.path}")

    def load(self) -> pd.DataFrame:
        if not self.path.exists():
            raise ProviderUnavailable(self.status().message)
        return self.parse(self.path.read_text(encoding="utf-8-sig"))

    @classmethod
    def parse(cls, csv_text: str) -> pd.DataFrame:
        raw = pd.read_csv(io.StringIO(csv_text), dtype=str)
        lower = {c.strip().lower(): c for c in raw.columns}

        def pick(field: str) -> Optional[pd.Series]:
            for alias in cls._ALIASES[field]:
                if alias in lower:
                    return raw[lower[alias]]
            return None

        code, name, isin = pick("symbol"), pick("company_name"), pick("isin")
        if code is None or name is None or isin is None:
            raise ProviderUnavailable("BSE scrip file must contain Security Code, Issuer/Security Name "
                                      "and ISIN columns.")
        df = pd.DataFrame({
            "company_name": name, "symbol": code, "isin": isin, "series": pick("series"),
            "listing_date": None, "face_value": pd.to_numeric(pick("face_value"), errors="coerce"),
        })
        status = pick("status")
        if status is not None:
            df = df[status.str.strip().str.lower().fillna("active") == "active"]
        return _clean_frame(df)

    def source(self) -> Source:
        modified = (datetime.fromtimestamp(self.path.stat().st_mtime, tz=timezone.utc)
                    if self.path.exists() else datetime.now(timezone.utc))
        return Source(
            title="BSE — List of scrips (downloaded by user)",
            provider="BSE Limited",
            kind=SourceKind.OFFICIAL_EXCHANGE,
            url=self.download_page,
            retrieved_at=modified,
            notes=f"Local file {self.path.name}; retrieval time is the file's modification time.",
        )

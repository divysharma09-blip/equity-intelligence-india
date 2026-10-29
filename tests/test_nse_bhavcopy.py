import io
import zipfile
from datetime import date

import pytest

from eii.models import SecurityIdentity
from eii.providers.base import ProviderUnavailable
from eii.providers.nse_bhavcopy import NSEBhavcopyProvider, parse_bhavcopy, pick_row

HEADER = ("TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,"
          "StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,"
          "SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,"
          "Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4")


def _row(isin, sym, series, close):
    return (f"2026-10-28,2026-10-28,CM,NSE,STK,1,{isin},{sym},{series},,,,,NAME,1,1,1,{close},1,1,,{close},"
            ",,100,100,1,F1,1,,,,,")


def _zip(*rows) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("bhav.csv", "\n".join([HEADER, *rows]) + "\n")
    return buf.getvalue()


RAW = _zip(_row("INE351F01018", "JPPOWER", "EQ", "12.97"),
           _row("INE351F01018", "JPPOWER", "BL", "13.50"),
           _row("INE002A01018", "RELIANCE", "EQ", "1400.10"))


def test_pick_prefers_equity_series_and_isin():
    df = parse_bhavcopy(RAW)
    row = pick_row(df, "JPPOWER", "INE351F01018")
    assert row["SctySrs"] == "EQ" and row["ClsPric"] == pytest.approx(12.97)
    assert pick_row(df, "RENAMED", "INE002A01018")["TckrSymb"] == "RELIANCE"  # symbol changed, ISIN still matches
    assert pick_row(df, "NOPE", None) is None


class _Resp:
    def __init__(self, status, content=b""):
        self.status_code, self.content = status, content


class _Session:
    """Serves the bhavcopy for one date only; every other date is a 404."""
    def __init__(self, day: date):
        self.day, self.headers, self.calls = day, {}, 0

    def get(self, url, timeout):
        self.calls += 1
        return _Resp(200, RAW) if f"{self.day:%Y%m%d}" in url else _Resp(404)


def test_fetch_walks_back_to_last_trading_day_and_caches(tmp_path, monkeypatch):
    import eii.providers.nse_bhavcopy as mod
    monkeypatch.setattr(mod, "datetime", _FixedNow)
    session = _Session(date(2026, 10, 28))
    p = NSEBhavcopyProvider(tmp_path, session=session)
    ident = SecurityIdentity(company_name="Jaiprakash Power Ventures Limited", nse_symbol="JPPOWER",
                             isin="INE351F01018")
    bundle = p.fetch(ident)
    assert bundle.market.price == pytest.approx(12.97) and bundle.market.currency == "INR"
    assert bundle.market.as_of.date() == date(2026, 10, 28)
    assert (tmp_path / "bhavcopy" / "20261028.csv.zip").exists()
    calls = session.calls
    NSEBhavcopyProvider(tmp_path, session=session).fetch(ident)  # served from disk cache
    assert session.calls == calls + 1  # only today's (still missing) file is retried


def test_fetch_unknown_symbol_is_unavailable(tmp_path, monkeypatch):
    import eii.providers.nse_bhavcopy as mod
    monkeypatch.setattr(mod, "datetime", _FixedNow)
    p = NSEBhavcopyProvider(tmp_path, session=_Session(date(2026, 10, 28)))
    with pytest.raises(ProviderUnavailable):
        p.fetch(SecurityIdentity(company_name="X", nse_symbol="NOTLISTED"))


from datetime import datetime as _dt  # noqa: E402


class _FixedNow(_dt):
    @classmethod
    def now(cls, tz=None):
        return _dt(2026, 10, 29, 10, 0, tzinfo=tz)

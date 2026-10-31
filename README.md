# Equity Intelligence India

A research workspace for Indian listed equities. It checks a company's identity against
official exchange data, gathers whatever data the configured providers can supply, and
organises it into a transparent research framework. It also stores every completed
report in SQLite and in an Excel workbook.

> **This is a research tool, not investment advice.** The scorecard is a transparent
> research framework. It is not a scientifically validated prediction. The app never
> issues buy or sell recommendations. It withholds any conclusion when data is
> incomplete or unverified.

## Quick start

```bash
cd equity-intelligence-india
python3.12 -m venv .venv            # or: uv venv .venv --python 3.12
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                # optional; defaults work without it
streamlit run app.py                # opens http://localhost:8501
pytest                              # run the test suite
```

No API keys are needed. The first search downloads NSE's official equity list (about 180 KB) and caches it for 24 hours.

## What it does

| Page | Purpose |
|---|---|
| **Search** | Find companies by name, NSE symbol, BSE code (6 digits) or ISIN. Results are *candidates* until they are verified. |
| **Research workspace** | Eight sections: Overview · Business & moat · Financials · Quality/Growth/Valuation/Strength · Governance & risks · Valuation scenarios · Four investors · Sources & data quality. Ends with **Complete & save report…**, which asks *Yes/No* before writing anything. |
| **Saved companies** | Every saved company and its report versions, with an audit trail. You can reopen any version. |
| **Compare** | Key metrics side by side and a factor-profile radar chart for up to 6 companies. Warns when companies come from different sectors. |
| **Settings & methodology** | Provider status, NSE refresh, BSE list upload, scorecard weights, Excel sync retry, database backup, full methodology. |

## Four-investor analysis (Buffett · Munger · Dalio · Fisher)

The **Four investors** tab scores the company against the master prompt in
`eii/four_investor/master_prompt.md`: 77 parameters, rated 0–4, 25 points per category,
100 per investor, combined at equal weight.

- **App ratings, live for every company** (typically 80–100% coverage per investor, no Claude run):
  - *Filed statements* (NSE XBRL): ROCE/ROE and their persistence, margins and their stability, gross
    margin, FCF, CFO/PAT, leverage, interest cover, liquidity, short-term debt share, incremental ROCE,
    loss years, FY21 (COVID) revenue change, FY23 (inflation) margin change, input-cost share, dilution,
    payout, capex/depreciation, growth vs nominal GDP.
  - *Public NSE evidence* (`eii/four_investor/evidence.py`): BRSR (exports, R&D, related-party share,
    attrition, dealer network), 3 years of announcements (fraud/default/insolvency, auditor resignations,
    investor calls and presentations, acquisitions, buybacks, rating actions), promoter-holding trend,
    year-end P/E history from bhavcopy prices, index tier.
  - *DCF model*: base and bear two-stage FCF DCF (12% discount, 5% terminal) for value and margin of safety.
  - *Sector profiles* (`eii/four_investor/sector_profiles.py`): industry-level judgement for the 22 NSE
    sectors (GDP sensitivity, regulation, commodity exposure, disruption, market growth) and which
    parameters apply at all (e.g. patents are not applicable to a power utility; excluded from coverage).
  - Lenders get sector substitutes (CET1, NPAs, credit cost, NII/assets, cost-to-income).
- **Claude Code research** — everything else (moat, management, macro, innovation, valuation
  scenarios, red flags, the full 18-section report) comes from a run on your Mac, on your Claude plan,
  with no API key:

  ```bash
  python -m eii.four_investor JPPOWER            # symbol, ISIN or name; --dry-run prints the prompt
  ```

  It saves `data/four_investor/<ISIN>.json`. Commit and push it to show it on the deployed app.
  Precedence: app data > Claude research > app DCF model and sector profile. Unrated parameters are
  excluded, never scored as zero; not-applicable ones are left out of the coverage denominator.

## Data integrity rules (enforced in code)

* **Nothing is fabricated.** Missing values stay missing (shown as "—") and are excluded from scores, never counted as zero.
* **Every value carries a label:** *Reported by provider*, *Analyst-entered*, *Derived*, *Estimate*, *Assumption*, *AI-generated interpretation* (reserved), or *Missing*.
* **A name match is not proof of identity.** A company is *verified* only when all of these hold:
  * the ISIN passes the ISO 6166 check digit
  * NSE's official list maps the symbol to the same ISIN
  * the BSE list, if available, maps the code to the same ISIN
  * the data provider reports the same ticker and a matching company name

  Any disagreement → **conflict**.
* **Currency guard.** Yahoo reports some Indian companies' statements in USD (for example Infosys) while the share price is in INR. The app detects this. It disables price multiples, scenarios and DCF, and raises a critical warning. It does not mix currencies.
* **Conclusion gate.** The framework score and its interpretation are shown only when all of these hold:
  * identity is verified
  * at least 3 years of statements are available
  * scorecard coverage is at least 60%
  * a current price is available
  * there are no critical warnings

  Otherwise the app shows *"Insufficient verified data — no conclusion"*.
* **Sector-aware scorecard.** Every metric declares the company types it applies to:

  | Company type | Metrics used |
  |---|---|
  | Industrial | ROCE, EBIT margins, D/E, interest coverage, current ratio, cash conversion, EV/EBITDA, FCF yield |
  | Bank | ROA, NII/assets, cost-to-income, credit cost, CET1, GNPA, NNPA, NII and advances growth |
  | NBFC | ROA, NII/assets, credit cost, leverage, NII and loan growth |

  Metrics that don't apply to a company are excluded from both the score and the coverage figure. For banks and NBFCs, EV, FCF yield and DCF are not computed. NBFC GNPA and capital adequacy aren't in exchange filings, so they show as missing. **Insurers are not supported yet**, and the gate blocks any conclusion for them.
* **Filed beats analyst-entered.** Promoter pledge and audit opinion come from filings. The analyst's pledge entry is used only when no filing exists.

## Data providers

| Provider | Type | What it supplies |
|---|---|---|
| NSE equity list (`EQUITY_L.csv`) | Official exchange | Symbol, name and ISIN for search and identity checks. Cached for 24 h; falls back to the cache when offline. |
| **NSE corporate filings (XBRL)** | **Official company filings** | Primary source for annual statements, about 8 years (FY2019 onward):<ul><li>P&L, balance sheet and cash flow</li><li>For banks: GNPA/NNPA, CET1 and ROA</li><li>Shareholding pattern and promoter pledges</li><li>Auditor name and audit opinion</li><li>Corporate actions, used to restate EPS for splits and bonuses</li></ul> |
| Yahoo Finance via `yfinance` | **Unofficial** aggregator | Live price, company profile and 2-year price history. Also a statements fallback and a cross-check on filed figures. |
| BSE scrip list | Official exchange | BSE codes only. BSE blocks automated downloads (HTTP 403), so the list must be downloaded by hand and uploaded in **Settings**. |

**How NSE filings are processed** (`eii/providers/nse_filings.py`, `eii/providers/xbrl.py`):

**Fetching and parsing**
* Results filed up to December 2024 come from `/api/corporates-financial-results`. Filings from 2025 onward (SEBI Integrated Filing) come from `/api/integrated-filing-results`. Each year's annual figures come from its March-quarter filing.
* XBRL elements are matched by name, so the older and newer formats both parse. Periods are taken from what each filing declares. Older filings often have mis-dated or undefined contexts; for those, the parser falls back to NSE's conventions (`FourD` = full year, `OneI` = year end) and says so in the report.

**Statement basis and sector formats**
* One statement basis is used for every year, so bases are never mixed: consolidated, except standalone for banks. Consolidated bank filings tag GNPA and CET1 as 0.00.
* The latest filing's format (industrial, bank or NBFC) decides how every year is mapped.
* Insurer filings are detected but not mapped yet.

**Data checks**
* Lines combined from several filed figures are labelled *Derived*: EBIT, EBITDA, total debt, cash, NII and share count.
* A zero-filled headline field (for example, EPS of 0.00 next to a non-zero profit) is treated as missing.
* If net profit and EPS × shares disagree by more than 20%, both figures are dropped for that year. The FY2019 HDFC Bank filing is a real example.
* Filed revenue and profit are cross-checked against Yahoo with a 3% tolerance. Larger differences are flagged.
* Pledge percentages are checked against the share counts before they are used.

**Politeness**
* At least 1 s between requests, listings cached for 12 h, and XBRL documents cached permanently under `data/reference/filings/`.
* nseindia.com's terms restrict automated access. The app makes only a handful of requests per company for personal research. Turn it off with `EII_ENABLE_NSE_FILINGS=false`; the app then falls back to Yahoo for statements.

To add a provider, implement `SecurityMasterProvider` or `FundamentalsProvider` in
`eii/providers/base.py` and register it, in priority order, in `eii/providers/registry.py`.

## Storage

* **SQLite** `data/equity_research.db` is the source of truth. It is append-only: each save creates a new report version, and old versions are never overwritten. The `research_history` table records the audit trail.
* **Excel** `data/Equity_Intelligence_India.xlsx` has 10 sheets:
  * `Dashboard`, `Master_Companies`, `Financial_History`, `Forecasts`, `Valuation`
  * `Factor_Scores`, `Risks_Governance`, `Sources`, `Research_History`, `Settings_Methodology`

  `Master_Companies` is updated in place, one row per company. History sheets only gain rows. `Dashboard` and `Settings_Methodology` are regenerated on each save.

  Before each write the app copies the workbook to `data/backups/`, then saves atomically. Columns are located by header name, so sheets or rows you add yourself are kept.
* If Excel has the workbook open, the database save still completes. The report is flagged, and you can push it later with **Settings → Retry Excel sync**.

`data/*.db`, `data/backups/`, `data/reference/` and `logs/` are git-ignored.

## Project layout

```
app.py                      Streamlit entry point (st.navigation)
eii/
  config.py                 settings from env/.env, logging
  models.py                 Pydantic models (identity, facts, sources, reports)
  verification.py           ISIN check digit, exchange & provider identity checks
  backtest.py               contract + guards for future backtesting (not implemented)
  db/                       schema.sql, connection/backup, repository
  providers/                base interfaces, NSE/BSE masters, NSE filings + XBRL parser, Yahoo, registry
  calculations/             ratios, CAGR, momentum (pure functions)
  valuation/                bear/base/bull scenarios, DCF, reverse DCF
  scoring/                  configurable scorecard + conclusion gate
  services/                 search, report building, save orchestration
  export/                   Excel workbook manager
  ui/                       state/caching, components, views/*
tests/                      109 tests (pytest)
```

## Status

| Feature | Status |
|---|---|
| Search: name, NSE symbol, ISIN | ✅ Working (live NSE list) |
| Search: BSE code | ⚠️ Only after you upload the BSE scrip CSV |
| Identity verification | ✅ Working |
| **Statements from official NSE XBRL filings (about 8 years)** | ✅ Phase 2. Tested on TCS, Infosys, HDFC Bank, Bajaj Finance and HDFC Life. |
| **Split/bonus restatement of EPS** | ✅ Phase 2, from NSE corporate actions |
| **Bank and NBFC metrics** | ✅ Phase 2. Bank: GNPA, NNPA, CET1, ROA, cost-to-income, credit cost. NBFC: ROA, credit cost, leverage. |
| **Shareholding, promoter pledge, auditor and audit opinion from filings** | ✅ Phase 2 |
| **Cross-check of filed vs Yahoo figures** | ✅ Phase 2 |
| Live price, profile, price history | ✅ From an unofficial source (Yahoo) |
| Ratios, scorecard, conclusion gate, scenarios, DCF, reverse DCF | ✅ Working, tested. The DCF is disabled for lenders. |
| SQLite persistence and Excel workbook | ✅ Working, tested |
| NBFC Stage-3/GNPA and capital adequacy | ❌ Not in exchange XBRL. Would need annual-report parsing. |
| Insurer metrics (VNB, solvency, combined ratio) | ❌ Not implemented. The gate blocks conclusions. |
| Residual-income valuation for banks | ❌ Not implemented |
| Comparable-company valuation (peer-median implied value) | ❌ Not implemented |
| Quarterly results, related-party transaction analysis | ❌ Not implemented. The data exists in the integrated filings. |
| Analyst-consensus forecasts | ❌ Not implemented |
| Historical backtesting | ❌ Not implemented. Filing broadcast dates are now recorded, which is a prerequisite for point-in-time data. |
| AI-generated summaries | ❌ Not implemented |

"""Run the four-investor master prompt through Claude Code on this Mac and save the result.

    python -m eii.four_investor JPPOWER            # NSE symbol, ISIN or company name
    python -m eii.four_investor JPPOWER --horizon "5–10 years"

Uses the `claude` CLI on your Claude plan (no API key; ANTHROPIC_API_KEY is removed from the
environment so it can't be used by accident). Claude gets the app's verified data and the
ratings the app already computed, researches the rest on the web, and returns structured
ratings plus the full report. The run is saved to data/four_investor/<ISIN>.json; commit and
push it to show it on the deployed app.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from eii.calculations import facts_to_frame
from eii.claude_cli import claude_env, find_claude
from eii.config import get_settings
from eii.four_investor.evidence import collect_with_registry
from eii.four_investor.framework import PARAMS, RATING_LABELS, Rating, app_ratings, sector_ratings
from eii.four_investor.research import ClaudeAssessment, ResearchRun, save_run
from eii.providers import ProviderUnavailable, build_registry
from eii.services import build_report, compute_key_metrics, search_securities

MASTER_PROMPT = Path(__file__).with_name("master_prompt.md")
DEFAULT_MODEL = "claude-opus-5-5"
TIMEOUT_S = 60 * 60


def claude_bin() -> str:
    path = find_claude()
    if path is None:
        raise SystemExit("Claude Code CLI not found. Install it, or set CLAUDE_BIN to its path.")
    return path


def _crore(x):
    return None if x is None or x != x else round(x / 1e7, 2)


def app_data(report, km) -> dict:
    """The app's verified data, in the units an analyst reads (₹ crore, ratios as fractions)."""
    years: dict[str, dict[str, float]] = {}
    for f in sorted(report.financials, key=lambda f: f.period_end):
        if f.value is None:
            continue
        # Money in crore; per-share figures and ratios as filed; share counts in crore shares.
        scaled = f.value if ("/" in f.unit or f.unit == "ratio") else f.value / 1e7
        years.setdefault(f"{f.period_end:%Y-%m-%d}", {})[f.metric] = round(scaled, 4 if f.unit == "ratio" else 2)
    m = report.market
    o = report.ownership
    return {
        "company_type": report.company_type,
        "statement_basis": report.statement_basis,
        "identity_verification": report.verification.summary if report.verification else None,
        "market": None if m is None else {
            "price_inr": m.price, "as_of": m.as_of.isoformat() if m.as_of else None,
            "market_cap_crore": _crore(km.values.get("market_cap"))},
        "annual_financials_crore": years,
        "annual_financials_note": "Amounts in ₹ crore (share counts in crore shares); per-share figures in ₹; ratios as fractions.",
        "key_metrics": {k: v for k, v in km.values.items() if v is not None},
        "key_metric_notes": km.notes,
        "ownership": None if o is None else o.model_dump(mode="json"),
        "data_warnings": [f"[{w.severity}] {w.section}: {w.message}" for w in report.warnings],
        "sources": [{"title": s.title, "url": s.url} for s in report.sources],
    }


def build_user_prompt(report, km, fixed: dict[str, Rating], horizon: str,
                      by_sector: dict[str, Rating] | None = None) -> str:
    ident = report.identity
    rows = []
    for p in PARAMS:
        r, s = fixed.get(p.id), (by_sector or {}).get(p.id)
        if r and not r.applicable:
            app = "not applicable (lender)"
        elif r and r.source == "model" and r.rating is not None:
            app = (f"app DCF model: {r.rating} ({RATING_LABELS[r.rating]}) — {r.observed}; mechanical, basic shares, "
                   "no contingent liabilities: replace with your own valuation")
        elif r and r.rating is not None:
            app = f"{r.rating} ({RATING_LABELS[r.rating]}) — {r.observed}"
        elif s and not s.applicable:
            app = "sector profile: not applicable — rate only if company evidence shows it matters"
        elif s and s.rating is not None:
            app = f"sector profile: {s.rating} ({RATING_LABELS[s.rating]}) — replace with a company-specific rating"
        else:
            app = ""
        rows.append(f"| {p.id} | {p.investor} | {p.category} | {p.label} | {p.weight} | {app} |")
    return f"""Company name: {ident.company_name}
Stock ticker: {ident.nse_symbol or '—'}
ISIN: {ident.isin or '—'}
Exchange: NSE
Country: India
Investment horizon: {horizon}
Investment approach: Four-investor combined fundamental analysis.
Valuation preference: Intrinsic value and margin of safety.
Reporting currency: INR
Analysis date: {datetime.now(timezone.utc):%d %B %Y}

The company identity above is already verified against the official NSE equity list; do not ask for
clarification about which company this is.

## Data already verified by the research app (from NSE filings and the NSE closing price)

```json
{json.dumps(app_data(report, km), indent=1, default=str)}
```

Treat these figures as the primary dataset. Use web research (annual reports, NSE/BSE announcements,
investor presentations, concall transcripts, credit-rating reports, then reputable aggregators for
cross-checks) for everything else, and cite each material claim.

## Parameters

Every parameter of the framework, with its weight. Where the "App rating" column is filled, the app
has rated that parameter from filed data or NSE evidence; that rating is final and will be used as is.
"Sector profile" entries are generic industry judgements: assess those for this specific company.

| id | investor | category | parameter | weight | App rating |
|---|---|---|---|---|---|
{chr(10).join(rows)}

## What to return

Return JSON matching the provided schema:

- `assessments`: one entry for EVERY parameter id above that has no final app rating (including the
  sector-profile and DCF-model ones), using the exact ids.
  Rating 0–4 per the standardised methodology, or null with data_status "Unavailable" when the evidence
  is not there. Never use 0 for missing data.
- Investor scores in your report must be computed exactly as the app does: for each investor,
  sum(rating / 4 × weight) over rated parameters (app ratings plus yours), divided by the rated weight,
  × 100. Report coverage = rated weight / 100, and call a score provisional below 60% coverage.
- `business_quality_score`, `valuation_attractiveness_score`, `risk_score`, the two Dalio scores,
  `intrinsic_value_per_share` (₹ per share), `red_flags`, `verdict`, `verdict_summary`: per sections 13–20.
- `report_markdown`: the complete report in the exact 18-section sequence of section 21, in Markdown
  with tables, Indian number formatting (₹ crore) and inline source links.
- `sources`: the main sources you used, with URLs.
"""


def run_claude(system: str, user: str, model: str) -> ClaudeAssessment:
    cmd = [claude_bin(), "-p", "--model", model, "--effort", "high", "--output-format", "json",
           "--system-prompt", system, "--no-session-persistence", "--strict-mcp-config",
           "--setting-sources", "", "--tools", "WebSearch", "WebFetch",
           "--allowedTools", "WebSearch", "WebFetch",
           "--json-schema", json.dumps(ClaudeAssessment.model_json_schema())]
    with tempfile.TemporaryDirectory() as cwd:
        proc = subprocess.run(cmd, input=user, capture_output=True, text=True, timeout=TIMEOUT_S,
                              env=claude_env(), cwd=cwd)
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise SystemExit(f"claude CLI failed (exit {proc.returncode}): {proc.stderr[-800:] or proc.stdout[-800:]}")
    if result.get("is_error") or result.get("subtype") != "success":
        raise SystemExit(f"claude CLI error: {result.get('result') or result.get('subtype')}. "
                         "If this is a usage limit, wait for the reset and run again.")
    payload = result.get("structured_output") or json.loads(result["result"])
    return ClaudeAssessment.model_validate(payload)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m eii.four_investor", description=__doc__.split("\n\n")[0])
    ap.add_argument("query", help="NSE symbol, ISIN or company name")
    ap.add_argument("--horizon", default="3–5 years")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--dry-run", action="store_true", help="Print the prompt and exit without running Claude")
    args = ap.parse_args(argv)

    settings = get_settings()
    registry = build_registry(settings)
    nse = registry.nse.load()
    sources = [registry.nse.source()]
    try:
        bse = registry.bse.load()
        sources.append(registry.bse.source())
    except ProviderUnavailable:
        bse = None

    matches = search_securities(args.query, nse, bse)
    exact = [m for m in matches if m.matched_on in ("nse_symbol", "isin", "bse_code")]
    if len(exact) == 1 or len(matches) == 1:
        identity = (exact or matches)[0]
    elif not matches:
        raise SystemExit(f"No listed company matches '{args.query}'.")
    else:
        listing = "\n".join(f"  {m.nse_symbol or m.bse_code:<12} {m.isin or '':<14} {m.company_name}" for m in matches[:10])
        raise SystemExit(f"'{args.query}' matches several companies; run again with the symbol or ISIN:\n{listing}")

    print(f"Building verified data for {identity.company_name} ({identity.nse_symbol})…", flush=True)
    report = build_report(identity, registry, nse, bse, sources).report
    km = compute_key_metrics(report)
    price = report.market.price if report.market else None
    ev = collect_with_registry(identity, report.financials, km.values.get("pe"), registry, settings.reference_dir)
    fixed = app_ratings(km, report.company_type, facts_to_frame(report.financials), price, ev)
    _, by_sector = sector_ratings(ev.sector, report.company_type, ev, facts_to_frame(report.financials))
    user = build_user_prompt(report, km, fixed, args.horizon, by_sector)
    print(f"App rated {sum(r.rating is not None for r in fixed.values())} of {len(PARAMS)} parameters from data "
          f"(sector: {ev.sector or 'unknown'}).", flush=True)
    if args.dry_run:
        print(user)
        return
    print(f"Running Claude Code ({args.model}); this usually takes 10–30 minutes…", flush=True)

    assessment = run_claude(MASTER_PROMPT.read_text(encoding="utf-8"), user, args.model)
    run = ResearchRun(identity_key=identity.key, company_name=identity.company_name,
                      nse_symbol=identity.nse_symbol, generated_at=datetime.now(timezone.utc),
                      model=args.model, horizon=args.horizon,
                      price_used=report.market.price if report.market else None,
                      app_ratings=fixed, assessment=assessment)
    path = save_run(settings.data_dir, run)
    print(f"Saved {path}\nVerdict: {assessment.verdict}. Commit and push this file to show it on the deployed app.")


if __name__ == "__main__":
    sys.exit(main())

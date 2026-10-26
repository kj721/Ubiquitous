#!/usr/bin/env python3
"""
edgar_financials.py - Pull 10 years of normalized financial statements from SEC EDGAR.

Produces a JSON file that loads directly into the "Damodaran Valuation Tool" and
"Value Investor Tool" artifacts, plus optional CSVs of the three statements.

Standard library only. No API key. No pip install.

QUICK START
-----------
    python edgar_financials.py AAPL --email you@example.com

    # several companies at once, with CSVs, into a folder
    python edgar_financials.py AAPL MSFT KO --email you@example.com --csv --outdir ./data

    # supply the market price yourself (EDGAR has no price data)
    python edgar_financials.py AAPL --email you@example.com --price 268.50

    # or let the script try a free, keyless quote from Stooq
    python edgar_financials.py AAPL --email you@example.com --fetch-price

WHY --email IS REQUIRED
-----------------------
SEC requires a declared User-Agent containing a contact address and throttles to
10 requests/second. This script sends one and self-throttles. Without a real
address SEC may return 403. Set EDGAR_EMAIL in your environment to avoid
re-typing it.

DATA SOURCE
-----------
  https://www.sec.gov/files/company_tickers.json      ticker -> CIK
  https://data.sec.gov/submissions/CIK##########.json company profile, SIC code
  https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json   all XBRL facts

A NOTE ON WHAT THIS CAN AND CANNOT DO
-------------------------------------
XBRL tagging is company-specific. This script walks a prioritized list of tags
for each line item and records which one it actually used, in the "coverage"
block of the output, so you can always see the provenance of a number. Items
that no tag satisfies come back null rather than zero - the valuation tools
treat null as "unknown, ask the user" and zero as "genuinely zero", and the
difference matters.

Banks, insurers and REITs tag their statements very differently from industrial
companies. Expect gaps for those, and expect the valuation tools to warn you
that a FCFF model is the wrong instrument for a bank anyway.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ============================================================================
#  EDIT HERE  -  the only part of this file you need to touch
# ============================================================================
#
# Fill these in and you can just run the script with no arguments at all:
#
#     python edgar_financials.py
#
# Anything you type on the command line overrides what is set here, so
# `python edgar_financials.py MSFT` uses MSFT and ignores the TICKERS below.

# Your contact address. The SEC requires one in the request header and will
# refuse the request without it. It is sent only to sec.gov.
EMAIL = ""

# Which companies to pull. Add or remove tickers between the quotes.
# Class shares use a hyphen on EDGAR, e.g. "BRK-B".
TICKERS = ["AAPL"]

# Where to write the output. "." means the folder this script is in.
OUTPUT_FOLDER = "."

# Also write a CSV of each statement alongside the JSON?
WRITE_CSV = False

# Try to look up the current share price from a free source (Stooq).
# If this fails or you leave it False, just type the price into the tool.
LOOK_UP_PRICE = True

# ============================================================================
#  Nothing below here needs editing.
# ============================================================================

SCHEMA_VERSION = "1.0"

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
STOOQ_QUOTE_URL = "https://stooq.com/q/l/?s={sym}.us&f=sd2t2ohlcv&h&e=csv"

MIN_REQUEST_INTERVAL = 0.15  # seconds; SEC allows 10 req/s, we stay well under
_last_request_at = 0.0

ANNUAL_FORMS = ("10-K", "10-K/A", "10-KT", "20-F", "20-F/A", "40-F")
QUARTERLY_FORMS = ("10-Q", "10-Q/A")

# Annual durations land near 365 days but 52/53-week fiscal years drift.
ANNUAL_DAYS = (330, 400)
QUARTER_DAYS = (80, 100)


# ----------------------------------------------------------------------------
# Tag maps. Order matters: the first tag with data for a period wins.
# ----------------------------------------------------------------------------

INCOME_TAGS = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
        "SalesRevenueServicesNet",
        "RevenuesNetOfInterestExpense",
        "TotalRevenuesAndOtherIncome",
    ],
    "costOfRevenue": [
        "CostOfRevenue",
        "CostOfGoodsAndServicesSold",
        "CostOfGoodsSold",
        "CostOfServices",
        "CostOfSales",
    ],
    "grossProfit": ["GrossProfit"],
    "rnd": [
        "ResearchAndDevelopmentExpense",
        "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
    ],
    "sga": [
        "SellingGeneralAndAdministrativeExpense",
        "GeneralAndAdministrativeExpense",
    ],
    "sellingMarketing": [
        "SellingAndMarketingExpense",
        "MarketingExpense",
        "AdvertisingExpense",
    ],
    "operatingExpenses": ["OperatingExpenses", "CostsAndExpenses"],
    "ebit": [
        "OperatingIncomeLoss",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    ],
    "interestExpense": [
        "InterestExpense",
        "InterestExpenseDebt",
        "InterestAndDebtExpense",
        "InterestExpenseNonoperating",
        "InterestIncomeExpenseNet",
    ],
    "interestIncome": [
        "InvestmentIncomeInterest",
        "InterestAndDividendIncomeOperating",
        "InterestIncomeOther",
    ],
    "otherIncome": [
        "NonoperatingIncomeExpense",
        "OtherNonoperatingIncomeExpense",
    ],
    "pretaxIncome": [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic",
    ],
    "taxExpense": [
        "IncomeTaxExpenseBenefit",
        "CurrentIncomeTaxExpenseBenefit",
    ],
    "netIncome": [
        "NetIncomeLoss",
        "ProfitLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
    ],
    "minorityInterestIncome": [
        "NetIncomeLossAttributableToNoncontrollingInterest",
        "ProfitLossAttributableToNoncontrollingInterest",
    ],
}

INCOME_PER_SHARE_TAGS = {
    "epsBasic": ["EarningsPerShareBasic", "IncomeLossFromContinuingOperationsPerBasicShare"],
    "epsDiluted": [
        "EarningsPerShareDiluted",
        "EarningsPerShareBasicAndDiluted",
        "IncomeLossFromContinuingOperationsPerDilutedShare",
    ],
    "dividendsPerShare": [
        "CommonStockDividendsPerShareDeclared",
        "CommonStockDividendsPerShareCashPaid",
    ],
}

INCOME_SHARE_COUNT_TAGS = {
    "sharesBasic": [
        "WeightedAverageNumberOfSharesOutstandingBasic",
        "WeightedAverageNumberOfSharesOutstanding",
    ],
    "sharesDiluted": [
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingDiluted",
    ],
}

BALANCE_TAGS = {
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "CashAndDueFromBanks",
        "Cash",
    ],
    "shortTermInvestments": [
        "ShortTermInvestments",
        "MarketableSecuritiesCurrent",
        "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
        "OtherShortTermInvestments",
    ],
    "longTermInvestments": [
        "LongTermInvestments",
        "MarketableSecuritiesNoncurrent",
        "AvailableForSaleSecuritiesDebtSecuritiesNoncurrent",
    ],
    "receivables": [
        "AccountsReceivableNetCurrent",
        "ReceivablesNetCurrent",
        "AccountsAndOtherReceivablesNetCurrent",
        "AccountsReceivableGrossCurrent",
    ],
    "inventory": ["InventoryNet", "InventoryGross", "FIFOInventoryAmount"],
    "otherCurrentAssets": ["OtherAssetsCurrent"],
    "totalCurrentAssets": ["AssetsCurrent"],
    "ppeNet": ["PropertyPlantAndEquipmentNet"],
    "ppeGross": [
        "PropertyPlantAndEquipmentGross",
        "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetBeforeAccumulatedDepreciationAndAmortization",
    ],
    "accumulatedDepreciation": [
        "AccumulatedDepreciationDepletionAndAmortizationPropertyPlantAndEquipment",
    ],
    "operatingLeaseRightOfUseAsset": ["OperatingLeaseRightOfUseAsset"],
    "goodwill": ["Goodwill"],
    "intangibles": [
        "FiniteLivedIntangibleAssetsNet",
        "IntangibleAssetsNetExcludingGoodwill",
        "IndefiniteLivedIntangibleAssetsExcludingGoodwill",
    ],
    "otherAssetsNoncurrent": ["OtherAssetsNoncurrent", "OtherAssets"],
    "totalAssets": ["Assets"],
    "accountsPayable": [
        "AccountsPayableCurrent",
        "AccountsPayableAndAccruedLiabilitiesCurrent",
        "AccountsPayableTradeCurrent",
    ],
    "accruedLiabilities": [
        "AccruedLiabilitiesCurrent",
        "EmployeeRelatedLiabilitiesCurrent",
    ],
    "deferredRevenueCurrent": [
        "ContractWithCustomerLiabilityCurrent",
        "DeferredRevenueCurrent",
    ],
    "shortTermDebt": [
        "DebtCurrent",
        "ShortTermBorrowings",
        "CommercialPaper",
        "OtherShortTermBorrowings",
        "ShortTermNonBankLoansAndNotesPayable",
    ],
    "currentPortionLongTermDebt": [
        "LongTermDebtCurrent",
        "LongTermDebtAndCapitalLeaseObligationsCurrent",
    ],
    "operatingLeaseLiabilityCurrent": ["OperatingLeaseLiabilityCurrent"],
    "totalCurrentLiabilities": ["LiabilitiesCurrent"],
    "longTermDebt": [
        "LongTermDebtNoncurrent",
        "LongTermDebtAndCapitalLeaseObligations",
        "LongTermDebt",
        "LongTermNotesPayable",
    ],
    "operatingLeaseLiabilityNoncurrent": ["OperatingLeaseLiabilityNoncurrent"],
    "deferredTaxLiabilities": [
        "DeferredIncomeTaxLiabilitiesNet",
        "DeferredTaxLiabilitiesNoncurrent",
    ],
    "otherLiabilitiesNoncurrent": ["OtherLiabilitiesNoncurrent", "OtherLiabilities"],
    "totalLiabilities": ["Liabilities"],
    "preferredStock": ["PreferredStockValue", "PreferredStockValueOutstanding"],
    "minorityInterest": [
        "MinorityInterest",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "retainedEarnings": ["RetainedEarningsAccumulatedDeficit"],
    "treasuryStock": ["TreasuryStockValue", "TreasuryStockCommonValue"],
    "totalEquity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "CommonStockholdersEquity",
    ],
    "sharesOutstanding": [
        "CommonStockSharesOutstanding",
        "CommonStockSharesIssued",
    ],
}

CASHFLOW_TAGS = {
    "cfo": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    "cfi": [
        "NetCashProvidedByUsedInInvestingActivities",
        "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations",
    ],
    "cff": [
        "NetCashProvidedByUsedInFinancingActivities",
        "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations",
    ],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "PaymentsForCapitalImprovements",
        "PaymentsToAcquireOtherPropertyPlantAndEquipment",
    ],
    "depreciationAmortization": [
        "DepreciationDepletionAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "DepreciationAndAmortization",
        "Depreciation",
    ],
    "amortizationOfIntangibles": [
        "AmortizationOfIntangibleAssets",
        "AmortizationOfFiniteLivedIntangibleAssets",
    ],
    "stockComp": [
        "ShareBasedCompensation",
        "AllocatedShareBasedCompensationExpense",
        "ShareBasedCompensationArrangementByShareBasedPaymentAwardCompensationCost",
    ],
    "deferredTaxes": [
        "DeferredIncomeTaxExpenseBenefit",
        "DeferredIncomeTaxesAndTaxCredits",
    ],
    "changeInWorkingCapital": [
        "IncreaseDecreaseInOperatingCapital",
        "OtherOperatingActivitiesCashFlowStatement",
    ],
    "acquisitions": [
        "PaymentsToAcquireBusinessesNetOfCashAcquired",
        "PaymentsToAcquireBusinessesAndInterestInAffiliates",
    ],
    "dividendsPaid": [
        "PaymentsOfDividendsCommonStock",
        "PaymentsOfDividends",
        "PaymentsOfDistributionsToAffiliates",
    ],
    "buybacks": [
        "PaymentsForRepurchaseOfCommonStock",
        "PaymentsForRepurchaseOfEquity",
    ],
    "debtIssued": [
        "ProceedsFromIssuanceOfLongTermDebt",
        "ProceedsFromIssuanceOfDebt",
        "ProceedsFromNotesPayable",
    ],
    "debtRepaid": [
        "RepaymentsOfLongTermDebt",
        "RepaymentsOfDebt",
        "RepaymentsOfNotesPayable",
    ],
}

# Items reported as positive cash outflows that we keep as positive magnitudes.
POSITIVE_OUTFLOWS = {
    "capex", "acquisitions", "dividendsPaid", "buybacks", "debtRepaid",
}


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------

def _throttle() -> None:
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < MIN_REQUEST_INTERVAL:
        time.sleep(MIN_REQUEST_INTERVAL - elapsed)
    _last_request_at = time.monotonic()


def http_get(url: str, email: str, *, retries: int = 4, timeout: int = 60) -> bytes:
    """GET with the SEC-mandated User-Agent, throttling, and backoff on 429/5xx."""
    headers = {
        "User-Agent": f"edgar_financials.py ({email})",
        "Accept-Encoding": "gzip, deflate",
        "Accept": "application/json,text/csv,*/*",
    }
    last_error: Exception | None = None
    for attempt in range(retries):
        _throttle()
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                return raw
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code in (403, 429) or exc.code >= 500:
                wait = 2 ** attempt
                print(f"    HTTP {exc.code}; retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def http_get_json(url: str, email: str) -> dict:
    return json.loads(http_get(url, email).decode("utf-8"))


# ----------------------------------------------------------------------------
# Ticker -> CIK
# ----------------------------------------------------------------------------

def cache_path(name: str) -> Path:
    base = Path(os.environ.get("EDGAR_CACHE_DIR", Path.home() / ".edgar_cache"))
    base.mkdir(parents=True, exist_ok=True)
    return base / name


def load_ticker_map(email: str, *, max_age_days: int = 7) -> dict[str, dict]:
    path = cache_path("company_tickers.json")
    fresh = (
        path.exists()
        and (time.time() - path.stat().st_mtime) < max_age_days * 86400
        and path.stat().st_size > 1000
    )
    if not fresh:
        print("  fetching SEC ticker->CIK map")
        path.write_bytes(http_get(SEC_TICKERS_URL, email))

    raw = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for entry in raw.values():
        ticker = str(entry.get("ticker", "")).upper().strip()
        if not ticker:
            continue
        out[ticker] = {
            "cik": str(entry["cik_str"]).zfill(10),
            "title": entry.get("title", ""),
        }
    return out


def resolve_cik(ticker: str, tmap: dict[str, dict]) -> dict:
    key = ticker.upper().strip()
    if key in tmap:
        return tmap[key]
    # Class-share tickers appear as BRK-B on SEC and BRK.B elsewhere.
    for alt in (key.replace(".", "-"), key.replace("-", "."), key.replace(".", ""), key.replace("-", "")):
        if alt in tmap:
            return tmap[alt]
    raise KeyError(
        f"Ticker {ticker!r} not found in the SEC ticker list. "
        "Only SEC registrants are covered (no foreign-listed-only or private companies). "
        "Try the class-share form, e.g. BRK-B."
    )


# ----------------------------------------------------------------------------
# Fact extraction
# ----------------------------------------------------------------------------

def parse_date(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def _is_monetary_unit(unit: str) -> bool:
    return unit == "USD" or (len(unit) == 3 and unit.isalpha() and unit.isupper())


def collect_facts(companyfacts: dict, tag: str) -> list[dict]:
    """Return every reported observation of `tag` across us-gaap and dei."""
    out: list[dict] = []
    for taxonomy in ("us-gaap", "ifrs-full", "dei", "srt"):
        node = companyfacts.get("facts", {}).get(taxonomy, {}).get(tag)
        if not node:
            continue
        for unit, observations in node.get("units", {}).items():
            for obs in observations:
                rec = dict(obs)
                rec["_unit"] = unit
                rec["_taxonomy"] = taxonomy
                rec["_tag"] = tag
                out.append(rec)
    return out


def select_duration_facts(
    facts: list[dict], forms: tuple[str, ...], day_range: tuple[int, int], units: set[str] | None
) -> dict[tuple[str, str], dict]:
    """Keyed by (start, end). Keeps the most recently *filed* value, i.e. restated."""
    best: dict[tuple[str, str], dict] = {}
    lo, hi = day_range
    for f in facts:
        if "start" not in f or "end" not in f:
            continue
        if f.get("form") not in forms:
            continue
        if units is not None and f["_unit"] not in units:
            continue
        try:
            start, end = parse_date(f["start"]), parse_date(f["end"])
        except ValueError:
            continue
        days = (end - start).days
        if not (lo <= days <= hi):
            continue
        key = (f["start"], f["end"])
        prev = best.get(key)
        if prev is None or str(f.get("filed", "")) > str(prev.get("filed", "")):
            best[key] = f
    return best


def select_instant_facts(
    facts: list[dict], forms: tuple[str, ...] | None, units: set[str] | None
) -> dict[str, dict]:
    """Keyed by `end` date. Keeps the most recently filed value."""
    best: dict[str, dict] = {}
    for f in facts:
        if "start" in f or "end" not in f:
            continue
        if forms is not None and f.get("form") not in forms:
            continue
        if units is not None and f["_unit"] not in units:
            continue
        key = f["end"]
        prev = best.get(key)
        if prev is None or str(f.get("filed", "")) > str(prev.get("filed", "")):
            best[key] = f
    return best


class FactStore:
    """Indexes every tag we care about once, then answers per-period lookups."""

    def __init__(self, companyfacts: dict, currency: str = "USD"):
        self.cf = companyfacts
        self.currency = currency
        self._cache: dict[str, list[dict]] = {}

    def facts(self, tag: str) -> list[dict]:
        if tag not in self._cache:
            self._cache[tag] = collect_facts(self.cf, tag)
        return self._cache[tag]

    def annual_series(self, tags: list[str], units: set[str] | None) -> dict[str, tuple[float, str]]:
        """{period_end: (value, tag_used)} for annual durations, first tag wins."""
        out: dict[str, tuple[float, str]] = {}
        for tag in tags:
            picked = select_duration_facts(self.facts(tag), ANNUAL_FORMS, ANNUAL_DAYS, units)
            for (_start, end), f in picked.items():
                if end not in out and isinstance(f.get("val"), (int, float)):
                    out[end] = (float(f["val"]), tag)
        return out

    def instant_series(
        self, tags: list[str], units: set[str] | None, forms: tuple[str, ...] | None = ANNUAL_FORMS
    ) -> dict[str, tuple[float, str]]:
        out: dict[str, tuple[float, str]] = {}
        for tag in tags:
            picked = select_instant_facts(self.facts(tag), forms, units)
            for end, f in picked.items():
                if end not in out and isinstance(f.get("val"), (int, float)):
                    out[end] = (float(f["val"]), tag)
        return out

    def ytd_duration_facts(self, tags: list[str], units: set[str] | None) -> list[dict]:
        """All cumulative year-to-date spans from 10-Qs, newest first."""
        rows: list[dict] = []
        # Key on the tag as well as the span: several tags legitimately share a
        # period, and a span-only key would let the first tag scanned swallow it.
        seen: set[tuple[str, str, str]] = set()
        for tag in tags:
            for f in self.facts(tag):
                if "start" not in f or "end" not in f:
                    continue
                if f.get("form") not in QUARTERLY_FORMS:
                    continue
                if units is not None and f["_unit"] not in units:
                    continue
                key = (tag, f["start"], f["end"])
                if key in seen:
                    continue
                try:
                    days = (parse_date(f["end"]) - parse_date(f["start"])).days
                except ValueError:
                    continue
                if days < 80 or days > 310:
                    continue
                seen.add(key)
                rows.append({**f, "_days": days})
        rows.sort(key=lambda r: (r["end"], r.get("filed", "")), reverse=True)
        return rows


# ----------------------------------------------------------------------------
# Normalization
# ----------------------------------------------------------------------------

def fiscal_year_label(end_iso: str) -> int:
    """Companies name a fiscal year for the calendar year it ends in."""
    return parse_date(end_iso).year


def build_annual_statements(store: FactStore, units: set[str], years: int) -> tuple[list[dict], dict]:
    coverage: dict[str, dict] = {}

    income_series = {
        field: store.annual_series(tags, units)
        for field, tags in INCOME_TAGS.items()
    }
    per_share_series = {
        field: store.annual_series(tags, {"USD/shares"})
        for field, tags in INCOME_PER_SHARE_TAGS.items()
    }
    share_count_series = {
        field: store.annual_series(tags, {"shares"})
        for field, tags in INCOME_SHARE_COUNT_TAGS.items()
    }
    balance_series = {
        field: store.instant_series(tags, {"shares"} if field == "sharesOutstanding" else units)
        for field, tags in BALANCE_TAGS.items()
    }
    cashflow_series = {
        field: store.annual_series(tags, units)
        for field, tags in CASHFLOW_TAGS.items()
    }

    # The spine of the dataset is every fiscal year for which revenue or net
    # income was reported in an annual filing.
    period_ends: set[str] = set()
    for key in ("revenue", "netIncome", "ebit"):
        period_ends |= set(income_series.get(key, {}).keys())
    ordered_ends = sorted(period_ends)[-years:]

    def pull(series: dict[str, dict[str, tuple[float, str]]], field: str, end: str,
             *, group: str) -> float | None:
        hit = series.get(field, {}).get(end)
        if hit is None:
            return None
        value, tag = hit
        if field in POSITIVE_OUTFLOWS:
            value = abs(value)
        if field == "interestExpense":
            value = abs(value)
        cov = coverage.setdefault(f"{group}.{field}", {"tag": tag, "years": 0})
        cov["years"] += 1
        if cov["tag"] != tag:
            cov.setdefault("alsoUsed", set())
            cov["alsoUsed"].add(tag)
        return value

    # Balance-sheet instants are dated to the fiscal year end, but a filing may
    # be a day or two off from the income-statement period end. Snap to nearest.
    balance_dates = sorted({d for s in balance_series.values() for d in s})

    def nearest_balance_date(end: str, tolerance_days: int = 7) -> str | None:
        if end in balance_dates:
            return end
        target = parse_date(end)
        best, best_gap = None, tolerance_days + 1
        for candidate in balance_dates:
            gap = abs((parse_date(candidate) - target).days)
            if gap < best_gap:
                best, best_gap = candidate, gap
        return best

    rows: list[dict] = []
    for end in ordered_ends:
        bdate = nearest_balance_date(end) or end
        income = {f: pull(income_series, f, end, group="income") for f in INCOME_TAGS}
        income.update({f: pull(per_share_series, f, end, group="income") for f in INCOME_PER_SHARE_TAGS})
        income.update({f: pull(share_count_series, f, end, group="income") for f in INCOME_SHARE_COUNT_TAGS})
        balance = {f: pull(balance_series, f, bdate, group="balance") for f in BALANCE_TAGS}
        cashflow = {f: pull(cashflow_series, f, end, group="cashflow") for f in CASHFLOW_TAGS}

        rows.append({
            "fy": fiscal_year_label(end),
            "periodEnd": end,
            "balanceSheetDate": bdate,
            "income": fill_income_gaps(income),
            "balance": fill_balance_gaps(balance),
            "cashflow": cashflow,
        })

    for cov in coverage.values():
        if "alsoUsed" in cov:
            cov["alsoUsed"] = sorted(cov["alsoUsed"])

    return rows, coverage


def fill_income_gaps(inc: dict) -> dict:
    """Derive the handful of income-statement lines that follow by arithmetic."""
    rev, cogs, gp = inc.get("revenue"), inc.get("costOfRevenue"), inc.get("grossProfit")
    if gp is None and rev is not None and cogs is not None:
        inc["grossProfit"] = rev - cogs
    if cogs is None and rev is not None and gp is not None:
        inc["costOfRevenue"] = rev - gp

    pretax, tax, ni = inc.get("pretaxIncome"), inc.get("taxExpense"), inc.get("netIncome")
    if pretax is None and ni is not None and tax is not None:
        inc["pretaxIncome"] = ni + tax
    if tax is None and pretax is not None and ni is not None:
        inc["taxExpense"] = pretax - ni

    if inc.get("ebit") is None:
        rev_, opex = inc.get("revenue"), inc.get("operatingExpenses")
        if rev_ is not None and opex is not None:
            inc["ebit"] = rev_ - opex

    pretax = inc.get("pretaxIncome")
    tax = inc.get("taxExpense")
    inc["effectiveTaxRate"] = (
        round(tax / pretax, 6) if pretax not in (None, 0) and tax is not None and pretax > 0 else None
    )
    return inc


def fill_balance_gaps(bal: dict) -> dict:
    short = bal.get("shortTermDebt") or 0.0
    current_ltd = bal.get("currentPortionLongTermDebt") or 0.0
    ltd = bal.get("longTermDebt") or 0.0
    has_any_debt = any(
        bal.get(k) is not None for k in ("shortTermDebt", "currentPortionLongTermDebt", "longTermDebt")
    )
    # DebtCurrent already includes the current portion of long-term debt; adding
    # both would double count, so take the larger rather than the sum.
    current_debt = max(short, current_ltd) if (short and current_ltd) else (short + current_ltd)
    bal["totalDebt"] = (current_debt + ltd) if has_any_debt else None

    op_lease = (bal.get("operatingLeaseLiabilityCurrent") or 0.0) + (
        bal.get("operatingLeaseLiabilityNoncurrent") or 0.0
    )
    bal["operatingLeaseLiability"] = op_lease or None
    if bal["totalDebt"] is not None:
        bal["totalDebtInclLeases"] = bal["totalDebt"] + op_lease

    cash = bal.get("cash")
    sti = bal.get("shortTermInvestments") or 0.0
    bal["cashAndInvestments"] = (cash + sti) if cash is not None else None

    if bal.get("totalLiabilities") is None:
        ta, te = bal.get("totalAssets"), bal.get("totalEquity")
        if ta is not None and te is not None:
            bal["totalLiabilities"] = ta - te

    tca, tcl = bal.get("totalCurrentAssets"), bal.get("totalCurrentLiabilities")
    bal["workingCapital"] = (tca - tcl) if (tca is not None and tcl is not None) else None
    if tca is not None and tcl is not None:
        non_cash_ca = tca - (cash or 0.0) - sti
        non_debt_cl = tcl - current_debt
        bal["nonCashWorkingCapital"] = non_cash_ca - non_debt_cl
    else:
        bal["nonCashWorkingCapital"] = None

    return bal


def compute_derived(row: dict) -> dict:
    """Metrics the valuation tools lean on. Recomputed client-side too, but
    having them here makes the JSON readable on its own."""
    inc, bal, cfs = row["income"], row["balance"], row["cashflow"]
    d: dict[str, float | None] = {}

    rev = inc.get("revenue")
    ebit = inc.get("ebit")
    d["grossMargin"] = safe_div(inc.get("grossProfit"), rev)
    d["operatingMargin"] = safe_div(ebit, rev)
    d["netMargin"] = safe_div(inc.get("netIncome"), rev)
    d["ebitda"] = (ebit + cfs["depreciationAmortization"]) if (
        ebit is not None and cfs.get("depreciationAmortization") is not None
    ) else None
    d["ebitdaMargin"] = safe_div(d["ebitda"], rev)

    tax_rate = inc.get("effectiveTaxRate")
    d["nopat"] = ebit * (1 - tax_rate) if (ebit is not None and tax_rate is not None) else None

    equity = bal.get("totalEquity")
    debt = bal.get("totalDebt")
    cash = bal.get("cashAndInvestments")
    if equity is not None and debt is not None:
        d["investedCapital"] = equity + debt - (cash or 0.0)
    else:
        d["investedCapital"] = None
    d["roic"] = safe_div(d["nopat"], d["investedCapital"])
    d["roe"] = safe_div(inc.get("netIncome"), equity)
    d["roa"] = safe_div(inc.get("netIncome"), bal.get("totalAssets"))

    cfo, capex = cfs.get("cfo"), cfs.get("capex")
    d["freeCashFlow"] = (cfo - capex) if (cfo is not None and capex is not None) else None
    d["capexToRevenue"] = safe_div(capex, rev)
    d["capexToDepreciation"] = safe_div(capex, cfs.get("depreciationAmortization"))
    d["rndToRevenue"] = safe_div(inc.get("rnd"), rev)
    d["sgaToRevenue"] = safe_div(inc.get("sga"), rev)
    d["debtToEquity"] = safe_div(debt, equity)
    d["currentRatio"] = safe_div(bal.get("totalCurrentAssets"), bal.get("totalCurrentLiabilities"))
    d["interestCoverage"] = safe_div(ebit, inc.get("interestExpense"))
    d["assetTurnover"] = safe_div(rev, bal.get("totalAssets"))
    d["bookValuePerShare"] = safe_div(equity, bal.get("sharesOutstanding") or inc.get("sharesDiluted"))
    d["payoutRatio"] = safe_div(cfs.get("dividendsPaid"), inc.get("netIncome"))
    return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in d.items()}


def safe_div(num, den):
    if num is None or den in (None, 0):
        return None
    try:
        return num / den
    except ZeroDivisionError:
        return None


def build_ttm(store: FactStore, annual: list[dict], units: set[str]) -> dict | None:
    """TTM = last full year + current YTD - prior-year YTD over the same span.

    Only flow items. Balance-sheet items come from the most recent instant.
    """
    if not annual:
        return None
    last_fy = annual[-1]
    last_fy_end = last_fy["periodEnd"]

    flow_tag_groups = {**INCOME_TAGS, **CASHFLOW_TAGS}
    ytd_rows = store.ytd_duration_facts(
        [t for tags in flow_tag_groups.values() for t in tags], units
    )
    if not ytd_rows:
        return None

    latest_end = ytd_rows[0]["end"]
    if latest_end <= last_fy_end:
        return None  # no quarters published since the 10-K

    def ytd_value(tags: list[str], end: str, start: str) -> float | None:
        for tag in tags:
            for row in ytd_rows:
                if row["_tag"] == tag and row["end"] == end and row["start"] == start:
                    return float(row["val"])
        return None

    # Current YTD span: starts the day after the prior fiscal year end.
    current_spans = [r for r in ytd_rows if r["end"] == latest_end]
    if not current_spans:
        return None
    current_start = min(r["start"] for r in current_spans)
    span_days = (parse_date(latest_end) - parse_date(current_start)).days

    # Matching prior-year span, allowing a week of 52/53-week drift.
    prior_candidates = [
        r for r in ytd_rows
        if 350 <= (parse_date(latest_end) - parse_date(r["end"])).days <= 380
        and abs((parse_date(r["end"]) - parse_date(r["start"])).days - span_days) <= 10
    ]
    if not prior_candidates:
        return None
    prior_end = max(r["end"] for r in prior_candidates)
    prior_start = min(r["start"] for r in prior_candidates if r["end"] == prior_end)

    income, cashflow = {}, {}
    for field, tags in INCOME_TAGS.items():
        fy_val = last_fy["income"].get(field)
        cur = ytd_value(tags, latest_end, current_start)
        pri = ytd_value(tags, prior_end, prior_start)
        if fy_val is not None and cur is not None and pri is not None:
            value = fy_val + cur - pri
            income[field] = abs(value) if field == "interestExpense" else value
        else:
            income[field] = None
    for field, tags in CASHFLOW_TAGS.items():
        fy_val = last_fy["cashflow"].get(field)
        cur = ytd_value(tags, latest_end, current_start)
        pri = ytd_value(tags, prior_end, prior_start)
        if fy_val is not None and cur is not None and pri is not None:
            value = fy_val + cur - pri
            cashflow[field] = abs(value) if field in POSITIVE_OUTFLOWS else value
        else:
            cashflow[field] = None

    # Latest balance sheet from any form, including 10-Qs.
    latest_balance: dict = {}
    for field, tags in BALANCE_TAGS.items():
        series = store.instant_series(
            tags, {"shares"} if field == "sharesOutstanding" else units,
            forms=ANNUAL_FORMS + QUARTERLY_FORMS,
        )
        if series:
            newest = max(series)
            latest_balance[field] = series[newest][0]
            latest_balance.setdefault("_asOf", newest)
            if newest > latest_balance.get("_asOf", ""):
                latest_balance["_asOf"] = newest
        else:
            latest_balance[field] = None

    as_of = latest_balance.pop("_asOf", None)

    row = {
        "periodEnd": latest_end,
        "ytdStart": current_start,
        "priorYtd": {"start": prior_start, "end": prior_end},
        "balanceSheetDate": as_of,
        "income": fill_income_gaps(income),
        "balance": fill_balance_gaps(latest_balance),
        "cashflow": cashflow,
    }
    row["derived"] = compute_derived(row)
    return row


# ----------------------------------------------------------------------------
# Price (optional, keyless)
# ----------------------------------------------------------------------------

def fetch_price_stooq(ticker: str, email: str) -> tuple[float | None, str | None]:
    sym = ticker.lower().replace(".", "-")
    try:
        text = http_get(STOOQ_QUOTE_URL.format(sym=sym), email, retries=2, timeout=20).decode("utf-8")
    except Exception as exc:
        print(f"    price lookup failed ({exc}); enter it in the tool instead", file=sys.stderr)
        return None, None
    rows = list(csv.DictReader(text.splitlines()))
    if not rows:
        return None, None
    close = rows[0].get("Close")
    date = rows[0].get("Date")
    if close in (None, "", "N/D"):
        return None, None
    try:
        return float(close), date
    except ValueError:
        return None, None


# ----------------------------------------------------------------------------
# Industry classification, so the tools can steer model choice
# ----------------------------------------------------------------------------

def classify(sic: str | None) -> dict:
    """Map an SIC code to the handful of attributes that change which valuation
    model is defensible. Deliberately coarse."""
    try:
        code = int(sic) if sic else 0
    except (TypeError, ValueError):
        code = 0

    def between(lo, hi):
        return lo <= code <= hi

    is_bank = between(6000, 6199) or between(6710, 6726)
    is_insurer = between(6300, 6411)
    is_reit = between(6798, 6798)
    is_utility = between(4900, 4991)
    is_financial = is_bank or is_insurer or is_reit

    if between(2833, 2836) or between(8731, 8734) or between(3826, 3827):
        group = "pharma_biotech"
    elif between(7370, 7379) or between(3570, 3579) or between(3670, 3679):
        group = "technology"
    elif is_bank:
        group = "banking"
    elif is_insurer:
        group = "insurance"
    elif is_reit:
        group = "reit"
    elif is_utility:
        group = "utilities"
    elif between(1300, 1399) or between(2900, 2999):
        group = "energy"
    elif between(2000, 2199) or between(2080, 2099):
        group = "consumer_staples"
    elif between(5200, 5999):
        group = "retail"
    elif between(3300, 3399) or between(1000, 1099):
        group = "materials"
    elif between(3700, 3799) or between(3400, 3569):
        group = "industrials"
    elif between(4800, 4899):
        group = "telecom"
    else:
        group = "general"

    # Years over which to amortize capitalized R&D, per Damodaran's convention
    # that the life should match how long the research keeps earning.
    rnd_life = {
        "pharma_biotech": 10,
        "technology": 5,
        "industrials": 10,
        "materials": 10,
        "energy": 10,
        "telecom": 10,
        "consumer_staples": 5,
        "retail": 3,
    }.get(group, 5)

    return {
        "sic": sic,
        "group": group,
        "isFinancial": is_financial,
        "isBank": is_bank,
        "isInsurer": is_insurer,
        "isREIT": is_reit,
        "isUtility": is_utility,
        "rndAmortizationYears": rnd_life,
    }


# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------

def build_dataset(ticker: str, email: str, years: int, price: float | None,
                  fetch_price: bool) -> dict:
    tmap = load_ticker_map(email)
    hit = resolve_cik(ticker, tmap)
    cik = hit["cik"]

    print(f"  CIK {cik} ({hit['title']})")
    submissions = http_get_json(SEC_SUBMISSIONS_URL.format(cik=cik), email)
    print("  fetching companyfacts (this is the big one)")
    companyfacts = http_get_json(SEC_COMPANYFACTS_URL.format(cik=cik), email)

    currency = "USD"
    units = {currency}
    store = FactStore(companyfacts, currency)

    annual, coverage = build_annual_statements(store, units, years)
    for row in annual:
        row["derived"] = compute_derived(row)
    print(f"  parsed {len(annual)} fiscal years: "
          f"{annual[0]['fy'] if annual else '-'}-{annual[-1]['fy'] if annual else '-'}")

    ttm = build_ttm(store, annual, units)
    if ttm:
        print(f"  TTM through {ttm['periodEnd']}")
    else:
        print("  TTM unavailable (no quarters since the last 10-K, or tag gaps)")

    dei_shares = store.instant_series(
        ["EntityCommonStockSharesOutstanding"], {"shares"},
        forms=ANNUAL_FORMS + QUARTERLY_FORMS,
    )
    shares_outstanding = None
    shares_as_of = None
    if dei_shares:
        shares_as_of = max(dei_shares)
        shares_outstanding = dei_shares[shares_as_of][0]
    if shares_outstanding is None and annual:
        shares_outstanding = annual[-1]["balance"].get("sharesOutstanding")

    price_date = None
    price_source = None
    if price is not None:
        price_source = "manual"
    elif fetch_price:
        price, price_date = fetch_price_stooq(ticker, email)
        price_source = "stooq" if price else None
        if price:
            print(f"  price {price} as of {price_date} (Stooq)")

    meta = {
        "schemaVersion": SCHEMA_VERSION,
        "ticker": ticker.upper(),
        "cik": cik,
        "companyName": submissions.get("name") or hit["title"],
        "exchange": (submissions.get("exchanges") or [None])[0],
        "sicDescription": submissions.get("sicDescription"),
        "fiscalYearEnd": submissions.get("fiscalYearEnd"),
        "currency": currency,
        "unitsNote": "All monetary values are in whole units of the reporting currency, not millions.",
        "sharesOutstanding": shares_outstanding,
        "sharesOutstandingAsOf": shares_as_of,
        "price": price,
        "priceDate": price_date,
        "priceSource": price_source,
        "marketCap": (price * shares_outstanding) if (price and shares_outstanding) else None,
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source": "SEC EDGAR XBRL company facts API",
        "classification": classify(submissions.get("sic")),
    }

    return {
        "meta": meta,
        "annual": annual,
        "ttm": ttm,
        "coverage": coverage,
        "warnings": sanity_warnings(annual, meta),
    }


def sanity_warnings(annual: list[dict], meta: dict) -> list[str]:
    warnings: list[str] = []
    if len(annual) < 5:
        warnings.append(
            f"Only {len(annual)} fiscal years parsed. Normalized-earnings and "
            "through-the-cycle methods want at least 7-10."
        )
    if meta["classification"]["isFinancial"]:
        warnings.append(
            "This is a financial-sector registrant. FCFF and invested-capital "
            "metrics are not meaningful for banks and insurers (debt is raw "
            "material, not financing). Use equity-side models: FCFE, dividend "
            "discount, or excess returns on book equity."
        )
    if meta["classification"]["isREIT"]:
        warnings.append(
            "REIT: depreciation is largely non-economic. Work from FFO/AFFO and "
            "NAV rather than a standard FCFF build."
        )
    if annual:
        last = annual[-1]
        if last["income"].get("revenue") is None:
            warnings.append("No revenue tag matched in the latest year - check the coverage block.")
        if last["income"].get("ebit") is None:
            warnings.append("No operating income tag matched - EBIT must be entered manually.")
        if last["cashflow"].get("capex") is None:
            warnings.append("No capex tag matched - reinvestment must be entered manually.")
        negatives = [r["fy"] for r in annual if (r["income"].get("ebit") or 0) < 0]
        if negatives:
            warnings.append(
                f"Negative operating income in {', '.join(map(str, negatives))}. "
                "A growth DCF needs an explicit path back to a target margin; "
                "normalized-earnings methods need a judgment call on what is normal."
            )
    if meta["price"] is None:
        warnings.append("No market price captured. Enter it in the tool to get upside/downside.")
    return warnings


# ----------------------------------------------------------------------------
# CSV output
# ----------------------------------------------------------------------------

def write_csvs(dataset: dict, outdir: Path, ticker: str) -> list[Path]:
    written = []
    sections = [
        ("income", "income_statement", list(INCOME_TAGS) + list(INCOME_PER_SHARE_TAGS) + list(INCOME_SHARE_COUNT_TAGS) + ["effectiveTaxRate"]),
        ("balance", "balance_sheet", list(BALANCE_TAGS) + ["totalDebt", "totalDebtInclLeases", "operatingLeaseLiability", "cashAndInvestments", "workingCapital", "nonCashWorkingCapital"]),
        ("cashflow", "cash_flow", list(CASHFLOW_TAGS)),
        ("derived", "key_metrics", None),
    ]
    years = [r["fy"] for r in dataset["annual"]]
    ttm = dataset.get("ttm")

    for key, label, fields in sections:
        if fields is None:
            fields = sorted({k for r in dataset["annual"] for k in r[key]})
        path = outdir / f"{ticker}_{label}.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            header = ["line_item"] + [f"FY{y}" for y in years] + (["TTM"] if ttm else [])
            w.writerow(header)
            for field in fields:
                row = [field]
                for r in dataset["annual"]:
                    row.append(r[key].get(field))
                if ttm:
                    row.append(ttm.get(key, {}).get(field))
                w.writerow(row)
        written.append(path)
    return written


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Pull 10 years of normalized financial statements from SEC EDGAR.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="The JSON output loads directly into the Damodaran Valuation Tool "
               "and the Value Investor Tool.",
    )
    p.add_argument("tickers", nargs="*", default=None,
                   help="One or more tickers, e.g. AAPL MSFT BRK-B. "
                        "Omit to use the TICKERS list at the top of this file.")
    p.add_argument("--email", default=None,
                   help="Contact address for the SEC User-Agent header. "
                        "Omit to use EMAIL at the top of this file, or EDGAR_EMAIL.")
    p.add_argument("--years", type=int, default=10, help="Fiscal years to keep (default 10)")
    p.add_argument("--outdir", default=None, help="Where to write output")
    p.add_argument("--price", type=float, default=None,
                   help="Current share price. Only valid with a single ticker.")
    p.add_argument("--fetch-price", action="store_true",
                   help="Try a free keyless quote from Stooq")
    p.add_argument("--csv", action="store_true", help="Also write one CSV per statement")
    p.add_argument("--indent", type=int, default=1, help="JSON indent (0 for compact)")
    args = p.parse_args(argv)

    # Command line wins; otherwise fall back to the EDIT HERE block, then to
    # the environment. This is what lets the script run with no arguments.
    tickers = args.tickers or TICKERS
    email = args.email or EMAIL.strip() or os.environ.get("EDGAR_EMAIL")
    outdir_raw = args.outdir or OUTPUT_FOLDER
    want_csv = args.csv or WRITE_CSV
    fetch_price = args.fetch_price or LOOK_UP_PRICE

    if not tickers:
        p.error(
            "No ticker given.\n"
            "  Either type one:   python edgar_financials.py AAPL\n"
            "  Or open this file and put it in the TICKERS list near the top."
        )
    if not email or "@" not in email:
        p.error(
            "SEC requires a real contact address in the request header, and will\n"
            "  refuse the request without one. Three ways to supply it:\n"
            "    1. Open this file and set EMAIL = \"you@example.com\" near the top\n"
            "    2. Pass it each time:  python edgar_financials.py AAPL --email you@example.com\n"
            "    3. Set it once:        setx EDGAR_EMAIL \"you@example.com\"   (then reopen PowerShell)"
        )
    if args.price is not None and len(tickers) > 1:
        p.error("--price applies to a single ticker. Use --fetch-price for a batch.")

    # "." should mean the folder holding this script, not wherever the shell
    # happens to be - double-clicking the file makes those differ.
    if outdir_raw == ".":
        outdir = Path(__file__).resolve().parent
    else:
        outdir = Path(outdir_raw).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    failures = 0
    for ticker in tickers:
        print(f"\n{ticker.upper()}")
        try:
            dataset = build_dataset(ticker, email, args.years, args.price, fetch_price)
        except Exception as exc:
            print(f"  FAILED: {exc}", file=sys.stderr)
            failures += 1
            continue

        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", ticker.upper())
        json_path = outdir / f"{safe}_financials.json"
        json_path.write_text(
            json.dumps(dataset, indent=args.indent or None, default=str),
            encoding="utf-8",
        )
        print(f"  wrote {json_path}")

        if want_csv:
            for path in write_csvs(dataset, outdir, safe):
                print(f"  wrote {path}")

        for warning in dataset["warnings"]:
            print(f"  ! {warning}")

    print(f"\nOutput folder: {outdir}")
    if failures:
        print(f"{failures} ticker(s) failed.", file=sys.stderr)
        return 1
    print("Done. Drop the _financials.json file into either valuation tool.")
    return 0


if __name__ == "__main__":
    # Double-clicking the file on Windows opens a console that closes the
    # instant the script ends, taking any error message with it. If there were
    # no command-line arguments, assume that is what happened and wait.
    interactive = len(sys.argv) == 1
    try:
        code = main()
    except SystemExit as exc:          # argparse errors land here
        code = exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:           # anything unexpected
        print(f"\nUnexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
        code = 1
    if interactive:
        try:
            input("\nPress Enter to close...")
        except EOFError:
            pass
    raise SystemExit(code)

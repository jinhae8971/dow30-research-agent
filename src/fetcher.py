"""Dow Jones 30 market data client using yfinance + multi-source constituents.

Sources (in priority order):
  1. Hardcoded seed list (Dow 30 changes <1x/year — safe for 2026)
  2. Wikipedia (fallback for sector metadata / post-rebalance validation)

This removes the 403 Forbidden failure caused by Wikipedia's UA policy.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from io import StringIO

import httpx
import pandas as pd
import yfinance as yf
from tenacity import retry, stop_after_attempt, wait_exponential

from .logging_setup import get_logger
from .models import StockMarket

log = get_logger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Dow Jones Industrial Average — hardcoded constituents (as of 2026-04)
# Official source: S&P Dow Jones Indices. Last rebalance: 2024-11 (NVDA, SHW in).
# When DJIA rebalances, update this list and bump __CONSTITUENTS_REV.
# ──────────────────────────────────────────────────────────────────────────────
__CONSTITUENTS_REV = "2026-04-18"

DOW30_SEED: list[dict] = [
    {"ticker": "AAPL", "name": "Apple",                    "sector": "Information Technology"},
    {"ticker": "AMGN", "name": "Amgen",                    "sector": "Health Care"},
    {"ticker": "AMZN", "name": "Amazon",                   "sector": "Consumer Discretionary"},
    {"ticker": "AXP",  "name": "American Express",         "sector": "Financials"},
    {"ticker": "BA",   "name": "Boeing",                   "sector": "Industrials"},
    {"ticker": "CAT",  "name": "Caterpillar",              "sector": "Industrials"},
    {"ticker": "CRM",  "name": "Salesforce",               "sector": "Information Technology"},
    {"ticker": "CSCO", "name": "Cisco Systems",            "sector": "Information Technology"},
    {"ticker": "CVX",  "name": "Chevron",                  "sector": "Energy"},
    {"ticker": "DIS",  "name": "Walt Disney",              "sector": "Communication Services"},
    {"ticker": "GS",   "name": "Goldman Sachs",            "sector": "Financials"},
    {"ticker": "HD",   "name": "Home Depot",               "sector": "Consumer Discretionary"},
    {"ticker": "HON",  "name": "Honeywell",                "sector": "Industrials"},
    {"ticker": "IBM",  "name": "IBM",                      "sector": "Information Technology"},
    {"ticker": "JNJ",  "name": "Johnson & Johnson",        "sector": "Health Care"},
    {"ticker": "JPM",  "name": "JPMorgan Chase",           "sector": "Financials"},
    {"ticker": "KO",   "name": "Coca-Cola",                "sector": "Consumer Staples"},
    {"ticker": "MCD",  "name": "McDonald's",               "sector": "Consumer Discretionary"},
    {"ticker": "MMM",  "name": "3M",                       "sector": "Industrials"},
    {"ticker": "MRK",  "name": "Merck",                    "sector": "Health Care"},
    {"ticker": "MSFT", "name": "Microsoft",                "sector": "Information Technology"},
    {"ticker": "NKE",  "name": "Nike",                     "sector": "Consumer Discretionary"},
    {"ticker": "NVDA", "name": "Nvidia",                   "sector": "Information Technology"},
    {"ticker": "PG",   "name": "Procter & Gamble",         "sector": "Consumer Staples"},
    {"ticker": "SHW",  "name": "Sherwin-Williams",         "sector": "Materials"},
    {"ticker": "TRV",  "name": "Travelers",                "sector": "Financials"},
    {"ticker": "UNH",  "name": "UnitedHealth Group",       "sector": "Health Care"},
    {"ticker": "V",    "name": "Visa",                     "sector": "Financials"},
    {"ticker": "VZ",   "name": "Verizon",                  "sector": "Communication Services"},
    {"ticker": "WMT",  "name": "Walmart",                  "sector": "Consumer Staples"},
]

DJIA_WIKI_URL = "https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average"
# Wikimedia User-Agent policy: identify your tool + contact.
# https://meta.wikimedia.org/wiki/User-Agent_policy
_WIKI_HEADERS = {
    "User-Agent": (
        "dow30-research-agent/1.0 "
        "(+https://github.com/jinhae8971/dow30-research-agent; "
        "contact: github issues)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _seed_dataframe() -> pd.DataFrame:
    """Return the hardcoded DJIA seed list as a normalized DataFrame."""
    df = pd.DataFrame(DOW30_SEED)
    df["ticker"] = df["ticker"].astype(str).str.replace(".", "-", regex=False).str.strip()
    log.info("using hardcoded DJIA constituents rev=%s (%d tickers)",
             __CONSTITUENTS_REV, len(df))
    return df[["ticker", "name", "sector"]]


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
def _fetch_djia_wikipedia() -> pd.DataFrame:
    """Secondary source: scrape current Dow 30 list from Wikipedia."""
    resp = httpx.get(DJIA_WIKI_URL, headers=_WIKI_HEADERS, follow_redirects=True, timeout=20.0)
    resp.raise_for_status()
    tables = pd.read_html(StringIO(resp.text))
    df = None
    for t in tables:
        cols_lower = [str(c).lower() for c in t.columns]
        if any("symbol" in c or "ticker" in c for c in cols_lower):
            df = t
            break
    if df is None:
        raise RuntimeError("could not find DJIA constituents table on Wikipedia")

    col_map = {}
    for c in df.columns:
        cl = str(c).lower().strip()
        if cl in ("symbol", "ticker"):
            col_map[c] = "ticker"
        elif cl in ("company",):
            col_map[c] = "name"
        elif "industry" in cl:
            col_map[c] = "sector"
    df = df.rename(columns=col_map)

    if "ticker" not in df.columns:
        raise RuntimeError("ticker column not found in DJIA Wikipedia table")
    if "name" not in df.columns:
        df["name"] = df["ticker"]
    if "sector" not in df.columns:
        df["sector"] = ""

    df["ticker"] = df["ticker"].astype(str).str.replace(".", "-", regex=False).str.strip()
    log.info("fetched %d DJIA constituents from Wikipedia", len(df))
    return df[["ticker", "name", "sector"]]


def _fetch_djia_constituents() -> pd.DataFrame:
    """Primary: hardcoded seed. Wikipedia only for validation/cross-check.

    Strategy: always return the hardcoded list (deterministic, network-free).
    Log a warning if Wikipedia disagrees on ticker set (but don't fail).
    """
    seed = _seed_dataframe()

    # Best-effort Wikipedia cross-check (never fatal)
    try:
        wiki = _fetch_djia_wikipedia()
        seed_set = set(seed["ticker"])
        wiki_set = set(wiki["ticker"])
        if seed_set != wiki_set:
            missing = wiki_set - seed_set
            extra = seed_set - wiki_set
            log.warning(
                "DJIA seed/Wikipedia drift: missing=%s extra=%s — "
                "consider updating DOW30_SEED (current rev=%s)",
                missing or "{}", extra or "{}", __CONSTITUENTS_REV,
            )
    except Exception as exc:  # noqa: BLE001
        log.info("Wikipedia cross-check skipped: %s", exc)

    return seed


def _recent_trading_dates(n: int) -> list[str]:
    """Return the last N trading dates by checking DIA (Dow ETF) data."""
    end = datetime.now()
    start = end - timedelta(days=n + 15)
    dia = yf.download("DIA", start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"),
                       progress=False, auto_adjust=True)
    if dia.empty:
        raise RuntimeError("could not fetch DIA data to determine trading dates")
    dates = sorted(dia.index.strftime("%Y-%m-%d").tolist())
    return dates[-n:] if len(dates) >= n else dates


def fetch_all_markets() -> tuple[list[StockMarket], str]:
    """Fetch Dow 30 constituents with latest market data."""
    constituents = _fetch_djia_constituents()
    tickers = constituents["ticker"].tolist()
    meta_by_ticker = {row["ticker"]: row for _, row in constituents.iterrows()}

    log.info("downloading market data for %d Dow 30 tickers...", len(tickers))
    data = yf.download(
        tickers,
        period="5d",
        progress=False,
        auto_adjust=True,
        group_by="ticker",
        threads=True,
    )

    if data.empty:
        raise RuntimeError("yfinance returned empty data for Dow 30")

    trading_date = data.index[-1].strftime("%Y-%m-%d")
    prev_date = data.index[-2].strftime("%Y-%m-%d") if len(data.index) >= 2 else None

    stocks: list[StockMarket] = []
    for ticker in tickers:
        meta = meta_by_ticker.get(ticker, {})
        try:
            ticker_data = data if len(tickers) == 1 else data[ticker]

            latest = ticker_data.iloc[-1]
            close = float(latest["Close"])
            if close <= 0 or pd.isna(close):
                continue

            volume = int(latest["Volume"]) if not pd.isna(latest["Volume"]) else 0
            trading_value = close * volume

            change_pct = 0.0
            if prev_date and len(ticker_data) >= 2:
                prev_close = float(ticker_data.iloc[-2]["Close"])
                if prev_close > 0 and not pd.isna(prev_close):
                    change_pct = (close - prev_close) / prev_close * 100.0

            stocks.append(
                StockMarket(
                    ticker=ticker,
                    name=meta.get("name", ticker),
                    sector=meta.get("sector", ""),
                    close=close,
                    open=float(latest["Open"]) if not pd.isna(latest["Open"]) else 0,
                    high=float(latest["High"]) if not pd.isna(latest["High"]) else 0,
                    low=float(latest["Low"]) if not pd.isna(latest["Low"]) else 0,
                    volume=volume,
                    trading_value=trading_value,
                    market_cap=0,
                    change_pct=change_pct,
                )
            )
        except (KeyError, IndexError):
            continue

    _enrich_market_caps(stocks)

    log.info("fetched %d Dow 30 stocks for %s", len(stocks), trading_date)
    return stocks, trading_date


def _enrich_market_caps(stocks: list[StockMarket]) -> None:
    """Best-effort market cap enrichment using yfinance fast_info."""
    ticker_symbols = [s.ticker for s in stocks]
    try:
        tickers_obj = yf.Tickers(" ".join(ticker_symbols))
        for s in stocks:
            try:
                info = tickers_obj.tickers.get(s.ticker)
                if info and hasattr(info, "fast_info"):
                    mcap = getattr(info.fast_info, "market_cap", None)
                    if mcap and mcap > 0:
                        s.market_cap = float(mcap)
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        log.warning("market cap enrichment failed: %s", exc)


def get_recent_trading_date() -> str:
    dates = _recent_trading_dates(1)
    return dates[-1]


def get_past_trading_date(days_back: int) -> str:
    dates = _recent_trading_dates(days_back + 1)
    if len(dates) <= days_back:
        raise RuntimeError(f"could not find {days_back} past trading days")
    return dates[-(days_back + 1)]

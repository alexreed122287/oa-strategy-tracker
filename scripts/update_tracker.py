#!/usr/bin/env python3
"""
Option Alpha Strategy Tracker — daily updater.

Data sources (in order):
  1. Tradier market-data API, if the TRADIER_TOKEN environment variable is set
     (in GitHub Actions this comes from the TRADIER_TOKEN repo secret).
     Set TRADIER_ENV=sandbox to use the sandbox endpoint.
  2. yfinance (Yahoo Finance) as a free fallback.

Each run it:
  1. Snapshots current option marks (mid of bid/ask, falls back to last).
  2. Locks the ENTRY PRICE as the position's net mark at the close of buy_date
     (first snapshot on/after that date). "entry_price" in trades.json overrides.
  3. Computes underlying technicals (RSI-14, SMA-20/50, MACD) and produces
     Option Alpha–style exit recommendations for open positions.
  4. Auto-closes positions at expiration using intrinsic value.
  5. Aggregates win rate, profit factor, expectancy, avg win/loss — per strategy,
     with an explicit breakout for LONG CALLS.

Outputs: data/dashboard.json (consumed by index.html) and data/history.json.
"""

import json
import os
import sys
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
TRADES_FILE = ROOT / "trades.json"
HISTORY_FILE = ROOT / "data" / "history.json"
DASHBOARD_FILE = ROOT / "data" / "dashboard.json"

MULTIPLIER = 100

TRADIER_TOKEN = os.environ.get("TRADIER_TOKEN", "").strip()
TRADIER_BASE = ("https://sandbox.tradier.com/v1"
                if os.environ.get("TRADIER_ENV", "").lower() == "sandbox"
                else "https://api.tradier.com/v1")

# Alpha Vantage: used ONLY to backfill entry prices for back-dated trades,
# via its HISTORICAL_OPTIONS endpoint (full chain as of a past date).
# Free tier is 25 requests/day — each backfill costs 1 request per
# symbol+date, and results are cached in history.json so it's a one-time cost.
ALPHAVANTAGE_KEY = os.environ.get("ALPHAVANTAGE_KEY", "").strip()

STRATEGY_LABELS = {
    "long_call": "Long Call",
    "long_put": "Long Put",
    "covered_call": "Covered Call",
    "cash_secured_put": "Cash-Secured Put",
    "put_credit_spread": "Put Credit Spread",
    "call_credit_spread": "Call Credit Spread",
    "call_debit_spread": "Call Debit Spread",
    "put_debit_spread": "Put Debit Spread",
    "iron_condor": "Iron Condor",
    "iron_butterfly": "Iron Butterfly",
    "straddle": "Long Straddle",
    "strangle": "Long Strangle",
    "calendar": "Calendar Spread",
}

CREDIT_STRATEGIES = {
    "put_credit_spread", "call_credit_spread", "iron_condor",
    "iron_butterfly", "cash_secured_put", "covered_call",
}


# ---------------------------------------------------------------- utilities

def today_str() -> str:
    return date.today().isoformat()


def load_json(path: Path, default):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, default=str)


def dte(expiration: str) -> int:
    return (date.fromisoformat(expiration) - date.today()).days


# =========================================================== data providers

_closes_cache: dict = {}
_chain_cache: dict = {}


def _tradier_get(path: str, params: dict):
    r = requests.get(
        TRADIER_BASE + path,
        params=params,
        headers={"Authorization": f"Bearer {TRADIER_TOKEN}",
                 "Accept": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def get_closes(symbol: str) -> pd.Series:
    """~6 months of daily closes, indexed by date."""
    if symbol in _closes_cache:
        return _closes_cache[symbol]
    series = pd.Series(dtype=float)
    if TRADIER_TOKEN:
        try:
            start = (date.today() - timedelta(days=200)).isoformat()
            data = _tradier_get("/markets/history", {
                "symbol": symbol, "interval": "daily",
                "start": start, "end": today_str(),
            })
            days = (data.get("history") or {}).get("day") or []
            if isinstance(days, dict):
                days = [days]
            if days:
                series = pd.Series(
                    [float(d["close"]) for d in days],
                    index=pd.to_datetime([d["date"] for d in days]),
                )
        except Exception as e:
            print(f"  ! Tradier history failed for {symbol}: {e}")
    if series.empty:
        try:
            import yfinance as yf
            df = yf.Ticker(symbol).history(period="6mo", auto_adjust=True)
            if not df.empty:
                series = df["Close"]
                series.index = series.index.tz_localize(None)
        except Exception as e:
            print(f"  ! yfinance history failed for {symbol}: {e}")
    _closes_cache[symbol] = series
    return series


def get_chain_marks(symbol: str, expiration: str) -> dict:
    """{(type, strike): mark} for one expiration."""
    key = (symbol, expiration)
    if key in _chain_cache:
        return _chain_cache[key]
    marks: dict = {}
    if TRADIER_TOKEN:
        try:
            data = _tradier_get("/markets/options/chains",
                                {"symbol": symbol, "expiration": expiration})
            options = (data.get("options") or {}).get("option") or []
            if isinstance(options, dict):
                options = [options]
            for o in options:
                bid = float(o.get("bid") or 0)
                ask = float(o.get("ask") or 0)
                last = float(o.get("last") or 0)
                mark = (bid + ask) / 2 if (bid > 0 and ask > 0) else (last if last > 0 else None)
                if mark is not None:
                    marks[(o["option_type"], float(o["strike"]))] = round(mark, 4)
        except Exception as e:
            print(f"  ! Tradier chain failed for {symbol} {expiration}: {e}")
    if not marks:
        try:
            import yfinance as yf
            chain = yf.Ticker(symbol).option_chain(expiration)
            for opt_type, table in (("call", chain.calls), ("put", chain.puts)):
                for _, row in table.iterrows():
                    bid = float(row.get("bid") or 0)
                    ask = float(row.get("ask") or 0)
                    last = float(row.get("lastPrice") or 0)
                    mark = (bid + ask) / 2 if (bid > 0 and ask > 0) else (last if last > 0 else None)
                    if mark is not None:
                        marks[(opt_type, float(row["strike"]))] = round(mark, 4)
        except Exception as e:
            print(f"  ! yfinance chain failed for {symbol} {expiration}: {e}")
    _chain_cache[key] = marks
    return marks


_av_cache: dict = {}


def historical_entry(trade: dict, on_date: str) -> float | None:
    """Net position mark at the close of a PAST date, from Alpha Vantage's
    HISTORICAL_OPTIONS chain. Returns None if unavailable."""
    if not ALPHAVANTAGE_KEY:
        return None
    symbol = trade["symbol"]
    key = (symbol, on_date)
    if key not in _av_cache:
        try:
            r = requests.get("https://www.alphavantage.co/query", params={
                "function": "HISTORICAL_OPTIONS", "symbol": symbol,
                "date": on_date, "apikey": ALPHAVANTAGE_KEY,
            }, timeout=60)
            r.raise_for_status()
            payload = r.json()
            rows = payload.get("data") or []
            if not rows:
                print(f"  ! Alpha Vantage: no chain for {symbol} @ {on_date} "
                      f"({payload.get('Information') or payload.get('Note') or 'empty'})")
            marks = {}
            for o in rows:
                bid = float(o.get("bid") or 0)
                ask = float(o.get("ask") or 0)
                mark = float(o.get("mark") or 0)
                px = (bid + ask) / 2 if (bid > 0 and ask > 0) else (mark if mark > 0 else None)
                if px is not None:
                    marks[(o["type"], float(o["strike"]), o["expiration"])] = round(px, 4)
            _av_cache[key] = marks
        except Exception as e:
            print(f"  ! Alpha Vantage backfill failed for {symbol} @ {on_date}: {e}")
            _av_cache[key] = {}
    marks = _av_cache[key]
    if not marks:
        return None
    total = 0.0
    for leg in trade["legs"]:
        exp = leg.get("expiration", trade["expiration"])
        m = marks.get((leg["type"], float(leg["strike"]), exp))
        if m is None:
            return None
        sign = 1 if leg["side"] == "long" else -1
        total += sign * m * leg.get("qty", 1)
    return round(total, 4)


# ------------------------------------------------------------- indicators

def technicals(symbol: str) -> dict:
    close = get_closes(symbol)
    if close.empty or len(close) < 30:
        return {}
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = (100 - 100 / (1 + rs)).iloc[-1]
    sma20 = close.rolling(20).mean().iloc[-1]
    sma50 = close.rolling(50).mean().iloc[-1] if len(close) >= 50 else np.nan
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    hist = (macd - macd.ewm(span=9, adjust=False).mean()).iloc[-1]
    f = lambda v, r=2: None if pd.isna(v) else round(float(v), r)
    return {"price": f(close.iloc[-1]), "rsi": f(rsi, 1),
            "sma20": f(sma20), "sma50": f(sma50), "macd_hist": f(hist, 3)}


# --------------------------------------------------------------- position

def position_mark(trade: dict) -> float | None:
    """Net mark per share: long legs +, short legs −."""
    total = 0.0
    for leg in trade["legs"]:
        marks = get_chain_marks(trade["symbol"], leg.get("expiration", trade["expiration"]))
        m = marks.get((leg["type"], float(leg["strike"])))
        if m is None:
            return None
        sign = 1 if leg["side"] == "long" else -1
        total += sign * m * leg.get("qty", 1)
    return round(total, 4)


def intrinsic_at(symbol: str, on_date: str, trade: dict) -> float | None:
    close = get_closes(symbol)
    if close.empty:
        return None
    eligible = close[close.index.normalize() <= pd.Timestamp(on_date)]
    if eligible.empty:
        return None
    spot = float(eligible.iloc[-1])
    total = 0.0
    for leg in trade["legs"]:
        k = float(leg["strike"])
        iv = max(spot - k, 0) if leg["type"] == "call" else max(k - spot, 0)
        sign = 1 if leg["side"] == "long" else -1
        total += sign * iv * leg.get("qty", 1)
    return round(total, 4)


# ------------------------------------------------------- recommendations

def recommendations(trade: dict, entry: float, mark: float, tech: dict) -> list[dict]:
    """Option Alpha–style management signals. Educational, not advice."""
    recs = []
    strat = trade["strategy"]
    is_credit = strat in CREDIT_STRATEGIES
    days_left = dte(trade["expiration"])

    if is_credit:
        credit = abs(entry)
        cost_to_close = abs(mark)
        pct_of_max = (credit - cost_to_close) / credit * 100 if credit else 0
        if pct_of_max >= 50:
            recs.append({"level": "action", "text":
                f"At {pct_of_max:.0f}% of max profit — Option Alpha guidance is to close credit trades at ~50% of max and redeploy."})
        if cost_to_close >= 2 * credit:
            recs.append({"level": "action", "text":
                "Loss ≥ 2× credit received — common mechanical stop for credit spreads. Consider closing or rolling."})
        if days_left <= 21:
            recs.append({"level": "warn", "text":
                f"{days_left} DTE — inside the 21-DTE window where gamma risk accelerates; manage or roll rather than hold to expiry."})
    else:
        pl_pct = (mark - entry) / entry * 100 if entry else 0
        if pl_pct >= 100:
            recs.append({"level": "action", "text":
                f"Up {pl_pct:.0f}% — consider taking profit or selling half to lock in the original debit."})
        elif pl_pct >= 50:
            recs.append({"level": "watch", "text":
                f"Up {pl_pct:.0f}% — profit-target zone. A trailing exit (e.g., give back no more than a third) protects gains."})
        if pl_pct <= -50:
            recs.append({"level": "action", "text":
                f"Down {abs(pl_pct):.0f}% — standard long-premium stop is −50%; theta will keep working against the position."})
        if days_left <= 21 and pl_pct < 0:
            recs.append({"level": "warn", "text":
                f"{days_left} DTE and underwater — time decay steepens from here; the probability of recovery drops sharply."})
        elif days_left <= 7:
            recs.append({"level": "warn", "text":
                f"{days_left} DTE — final-week gamma/theta zone. Close or roll unless you want an expiration outcome."})

    rsi, price = tech.get("rsi"), tech.get("price")
    sma20, sma50 = tech.get("sma20"), tech.get("sma50")
    macd_hist = tech.get("macd_hist")
    bullish = strat in ("long_call", "call_debit_spread", "put_credit_spread", "covered_call", "cash_secured_put")
    bearish = strat in ("long_put", "put_debit_spread", "call_credit_spread")

    if rsi is not None:
        if rsi >= 70 and bullish:
            recs.append({"level": "watch", "text":
                f"RSI {rsi:.0f} (overbought) — momentum stretched; a scale-out here often beats waiting for the pullback."})
        if rsi <= 30 and bullish:
            recs.append({"level": "warn", "text":
                f"RSI {rsi:.0f} (oversold) — underlying momentum has broken down against the position."})
        if rsi <= 30 and bearish:
            recs.append({"level": "watch", "text":
                f"RSI {rsi:.0f} (oversold) — bearish position has momentum, but bounces from oversold are common; consider partial profits."})
    if price is not None and sma20 is not None:
        if bullish and price < sma20:
            recs.append({"level": "warn", "text":
                "Underlying closed below its 20-day SMA — short-term trend no longer supports the bullish thesis."})
        if bearish and price > sma20:
            recs.append({"level": "warn", "text":
                "Underlying closed above its 20-day SMA — short-term trend turning against the bearish thesis."})
    if price is not None and sma50 is not None and bullish and price < sma50:
        recs.append({"level": "warn", "text":
            "Underlying is below its 50-day SMA — intermediate trend is down; bullish premium is fighting the tape."})
    if macd_hist is not None and bullish and macd_hist < 0:
        recs.append({"level": "watch", "text":
            "MACD histogram negative — bullish momentum fading on the daily chart."})

    if not recs:
        recs.append({"level": "hold", "text":
            "No exit triggers hit — thesis intact, time remaining, technicals aligned. Hold per plan."})
    return recs


# ------------------------------------------------------------- metrics

def bucket_metrics(closed: list[dict]) -> dict:
    wins = [t for t in closed if t["pl"] > 0]
    losses = [t for t in closed if t["pl"] <= 0]
    gross_win = sum(t["pl"] for t in wins)
    gross_loss = abs(sum(t["pl"] for t in losses))
    n = len(closed)
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / n * 100, 1) if n else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else (None if not gross_win else float("inf")),
        "avg_win": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else None,
        "expectancy": round(sum(t["pl"] for t in closed) / n, 2) if n else None,
        "total_pl": round(sum(t["pl"] for t in closed), 2),
    }


def strategy_insight(label: str, metrics: dict, open_positions: list[dict]) -> list[str]:
    """Option Alpha-style narrative read on one bucket (a strategy structure or
    a user-defined named group) — same logic whether it's 'Long Call' overall
    or a specific scan like '1 Samurai Scan First'."""
    notes = []
    wr, pf = metrics.get("win_rate"), metrics.get("profit_factor")
    if metrics["trades"] == 0:
        notes.append(f"No closed trades yet in {label} — metrics will populate as positions resolve.")
    else:
        if pf is not None and pf != float("inf"):
            if pf >= 1.5:
                notes.append(f"Profit factor {pf} — {label} is earning ${pf} for every $1 lost. Edge is real; protect it with consistent position sizing.")
            elif pf >= 1.0:
                notes.append(f"Profit factor {pf} — marginally profitable. Results here live or die on exit discipline: cutting losers at −50% and letting winners run past +100% is what moves this number.")
            else:
                notes.append(f"Profit factor {pf} — losses are outrunning wins in {label}. Review whether entries are chasing extended moves (buying calls after RSI > 70 is the most common leak).")
        if wr is not None and wr < 45 and (metrics.get("avg_win") or 0) < abs(metrics.get("avg_loss") or 0):
            notes.append("Win rate under 45% with average losses larger than average wins — that combination is unsustainable; tighten stops or buy more time (60+ DTE) so the thesis has room to play out.")
        if wr is not None and wr >= 55:
            notes.append(f"Win rate {wr}% is strong for long premium — most of the edge now comes from letting the biggest winners run.")
    theta_heavy = [p for p in open_positions if p["dte"] <= 21]
    if theta_heavy:
        notes.append(f"{len(theta_heavy)} open position(s) inside 21 DTE — decay is now the dominant force on these; each day held is a real cost.")
    winners = [p for p in open_positions if (p.get("pl") or 0) > 0]
    losers = [p for p in open_positions if (p.get("pl") or 0) < 0]
    if open_positions:
        open_wr = len(winners) / len(open_positions) * 100
        if losers and not winners:
            notes.append(f"All {len(losers)} open position(s) are underwater — no confirmation yet that this batch of entries is working.")
        elif winners and not losers:
            notes.append(f"All {len(winners)} open position(s) are in the green — thesis confirmed so far, but that can reverse fast with long premium.")
        elif open_wr < 35:
            notes.append(f"Only {open_wr:.0f}% of open positions are currently profitable — this batch is fighting the tape more than the historical win rate suggests.")
    return notes


# ---------------------------------------------------------------- main

def main():
    print(f"Data source: {'Tradier (' + TRADIER_BASE + ')' if TRADIER_TOKEN else 'Yahoo Finance fallback (no TRADIER_TOKEN set)'}")
    trades = load_json(TRADES_FILE, [])
    history = load_json(HISTORY_FILE, {"snapshots": {}, "entries": {}, "auto_closes": {}, "equity": []})
    run_date = today_str()

    open_out, closed_out = [], []

    for trade in trades:
        tid = trade["id"]
        symbol = trade["symbol"]
        contracts = trade.get("contracts", 1)
        print(f"• {tid} {symbol} {trade['strategy']}")

        entry = trade.get("entry_price")
        if entry is None:
            entry = history["entries"].get(tid)
        mark = position_mark(trade)

        if entry is None:
            # back-dated trade: pull the true buy-date close from Alpha Vantage
            if trade["buy_date"] < run_date:
                entry = historical_entry(trade, trade["buy_date"])
                if entry is not None:
                    history["entries"][tid] = entry
                    print(f"  entry backfilled from {trade['buy_date']} close: {entry}")
            # genuine same-day trade: lock today's close mark as entry.
            # (back-dated trades whose AV backfill hasn't resolved yet stay
            # unset here on purpose, so a failed/rate-limited backfill can't
            # silently mislock entry to today's price instead of buy_date's.)
            if entry is None and mark is not None and run_date == trade["buy_date"]:
                entry = mark
                history["entries"][tid] = entry
                print(f"  entry locked at close mark {entry}")

        if mark is not None:
            history["snapshots"].setdefault(tid, {})[run_date] = mark

        tech = technicals(symbol)

        if trade.get("status") == "closed" and trade.get("close_price") is not None:
            close_px = float(trade["close_price"])
            if entry is None:
                print("  ! closed trade missing entry price — skipped from stats")
                continue
            pl = (close_px - entry) * MULTIPLIER * contracts
            closed_out.append(_closed_record(trade, entry, close_px, trade.get("close_date"), pl, "manual"))
            continue

        if dte(trade["expiration"]) < 0:
            close_px = history["auto_closes"].get(tid)
            if close_px is None:
                close_px = intrinsic_at(symbol, trade["expiration"], trade)
                if close_px is not None:
                    history["auto_closes"][tid] = close_px
            if entry is not None and close_px is not None:
                pl = (close_px - entry) * MULTIPLIER * contracts
                closed_out.append(_closed_record(trade, entry, close_px, trade["expiration"], pl, "expired"))
            else:
                print("  ! could not resolve expired trade (missing entry or intrinsic)")
            continue

        pl = pl_pct = None
        if entry is not None and mark is not None:
            pl = round((mark - entry) * MULTIPLIER * contracts, 2)
            pl_pct = round((mark - entry) / abs(entry) * 100, 1) if entry else None
        snaps = history["snapshots"].get(tid, {})
        spark = [snaps[d] for d in sorted(snaps)][-30:]
        open_out.append({
            "id": tid, "symbol": symbol,
            "strategy": trade["strategy"],
            "strategy_label": STRATEGY_LABELS.get(trade["strategy"], trade["strategy"]),
            "group": trade.get("group"),
            "buy_date": trade["buy_date"], "expiration": trade["expiration"],
            "dte": dte(trade["expiration"]), "contracts": contracts,
            "legs": trade["legs"],
            "entry_price": entry, "mark": mark,
            "pl": pl, "pl_pct": pl_pct,
            "technicals": tech,
            "sparkline": spark,
            "recommendations": (recommendations(trade, entry, mark, tech)
                                if entry is not None and mark is not None else
                                [{"level": "watch", "text": "Awaiting first market-close snapshot to lock entry price."}]),
            "notes": trade.get("notes", ""),
        })

    by_strategy = {}
    for t in closed_out:
        by_strategy.setdefault(t["strategy"], []).append(t)
    strategy_metrics = {
        s: {"label": STRATEGY_LABELS.get(s, s), **bucket_metrics(ts)}
        for s, ts in by_strategy.items()
    }
    overall = bucket_metrics(closed_out)

    # named strategy groups (user-defined portfolios): closed metrics + open exposure
    group_names = sorted({t.get("group") for t in trades if t.get("group")})
    group_metrics = {}
    for g in group_names:
        g_closed = [t for t in closed_out if t.get("group") == g]
        g_open = [p for p in open_out if p.get("group") == g]
        g_metrics = bucket_metrics(g_closed)
        group_metrics[g] = {
            "label": g, **g_metrics,
            "open_positions": len(g_open),
            "open_pl": round(sum(p["pl"] for p in g_open if p["pl"] is not None), 2),
            "insights": strategy_insight(g, g_metrics, g_open),
        }

    lc_closed = by_strategy.get("long_call", [])
    lc_open = [p for p in open_out if p["strategy"] == "long_call"]
    lc_metrics = bucket_metrics(lc_closed)
    long_calls = {
        **lc_metrics,
        "open_positions": len(lc_open),
        "open_pl": round(sum(p["pl"] for p in lc_open if p["pl"] is not None), 2),
        "insights": strategy_insight("Long Call", lc_metrics, lc_open),
    }

    realized = sum(t["pl"] for t in closed_out)
    open_pl = sum(p["pl"] for p in open_out if p["pl"] is not None)
    equity_point = {"date": run_date, "realized": round(realized, 2),
                    "total": round(realized + open_pl, 2)}
    history["equity"] = [e for e in history["equity"] if e["date"] != run_date] + [equity_point]
    history["equity"].sort(key=lambda e: e["date"])

    dashboard = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "as_of": run_date,
        "source": "tradier" if TRADIER_TOKEN else "yahoo",
        "overall": overall,
        "long_calls": long_calls,
        "strategies": strategy_metrics,
        "groups": group_metrics,
        "open": sorted(open_out, key=lambda p: p["dte"]),
        "closed": sorted(closed_out, key=lambda t: t["close_date"] or "", reverse=True),
        "equity": history["equity"][-120:],
    }

    save_json(HISTORY_FILE, history)
    save_json(DASHBOARD_FILE, dashboard)
    print(f"\nDashboard written: {len(open_out)} open, {len(closed_out)} closed.")


def _closed_record(trade, entry, close_px, close_date, pl, how):
    return {
        "id": trade["id"], "symbol": trade["symbol"],
        "strategy": trade["strategy"],
        "strategy_label": STRATEGY_LABELS.get(trade["strategy"], trade["strategy"]),
        "group": trade.get("group"),
        "buy_date": trade["buy_date"], "expiration": trade["expiration"],
        "close_date": close_date, "contracts": trade.get("contracts", 1),
        "entry_price": entry, "close_price": close_px,
        "pl": round(pl, 2),
        "pl_pct": round((close_px - entry) / abs(entry) * 100, 1) if entry else None,
        "closed_by": how,
    }


if __name__ == "__main__":
    sys.exit(main())

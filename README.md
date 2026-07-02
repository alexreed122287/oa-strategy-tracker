# Strategy Ledger — Option Alpha Tracker

A GitHub Pages dashboard that tracks Option Alpha–style option strategies, updates
itself **every weekday after US market close**, and reports win rate, profit factor,
expectancy, and rule-based exit recommendations — with an explicit breakout for
**long calls**.

**How it works**

```
trades.json  ──►  GitHub Action (weekdays ~4:20–5:20pm ET)
                     ├─ pulls option marks + underlying data (Yahoo Finance)
                     ├─ locks entry price at the buy-date close
                     ├─ computes RSI / SMA20 / SMA50 / MACD + exit signals
                     ├─ auto-closes expired positions at intrinsic value
                     └─ writes data/dashboard.json + data/history.json
index.html   ──►  GitHub Pages renders the dashboard from data/dashboard.json
```

## Setup — one command in a Codespace

Upload this folder to a Codespace (or any machine with an authenticated
`gh` CLI) and run:

```bash
bash setup.sh
```

The script creates the repo, commits and pushes everything, grants the
workflow write permission, enables GitHub Pages, prompts you for your
**Tradier API token** (stored as the encrypted `TRADIER_TOKEN` repo secret —
it never touches disk or history), and triggers the first update. Your
dashboard goes live at `https://<username>.github.io/<repo>/`.

### Manual setup (if you prefer clicking)

1. Create a repo and push these files to `main`.
2. Settings → Pages → Deploy from branch `main`, folder `/ (root)`.
3. Settings → Actions → General → Workflow permissions → **Read and write**.
4. Settings → Secrets and variables → Actions → New secret `TRADIER_TOKEN`
   with your Tradier API token (optional — omits to Yahoo fallback).
5. Actions tab → *Market-close update* → *Run workflow*.

## Data source

With `TRADIER_TOKEN` set, marks come from the **Tradier market-data API**
(real bid/ask mids, plus daily history for indicators). Set `TRADIER_ENV=sandbox`
in the workflow env if your token is a sandbox key. Without a token, the
tracker falls back to Yahoo Finance automatically. Scheduled runs happen
every weekday at 21:20 UTC — after the 4:00pm ET close year-round.

## Adding trades

Edit `trades.json` (directly on GitHub is fine). Each trade:

```json
{
  "id": "LC-SPY-001",
  "strategy": "long_call",
  "symbol": "SPY",
  "buy_date": "2026-07-01",
  "expiration": "2026-08-21",
  "contracts": 1,
  "legs": [ { "type": "call", "side": "long", "strike": 620, "qty": 1 } ],
  "status": "open",
  "notes": ""
}
```

- **Entry price** is recorded automatically as the position's net mark at the
  market close of `buy_date` (the first update run on/after that date).
  Yahoo doesn't provide historical option prices, so **add trades on or before
  their buy date**. For back-dated trades, set `"entry_price": 8.45` manually.
- **Multi-leg strategies**: list every leg. Net marks follow the convention
  long = +, short = −, so credit trades show a negative entry (credit received).
- **Closing a trade yourself**: set `"status": "closed"`, `"close_price": 4.20`
  (net, same sign convention), `"close_date": "2026-07-15"`.
- **Expiration**: open trades past expiry are auto-closed at intrinsic value.

Supported `strategy` values: `long_call`, `long_put`, `call_debit_spread`,
`put_debit_spread`, `put_credit_spread`, `call_credit_spread`, `iron_condor`,
`iron_butterfly`, `covered_call`, `cash_secured_put`, `straddle`, `strangle`,
`calendar`.

## What the exit signals check

Rule-based, in the spirit of Option Alpha's mechanical management:

| Signal | Debit trades (long calls/puts, debit spreads) | Credit trades |
|---|---|---|
| Profit target | flag at +50%, action at +100% | action at 50% of max profit |
| Stop | action at −50% of debit | action when loss ≥ 2× credit |
| Time | warn at ≤21 DTE if losing; warn ≤7 DTE always | warn at ≤21 DTE (gamma window) |
| RSI-14 | overbought/oversold vs. position direction | same |
| Trend | close vs. SMA-20 and SMA-50 | same |
| Momentum | MACD histogram vs. position direction | same |

## Metrics

Per strategy and overall: **win rate**, **profit factor** (gross wins ÷ gross
losses), **average win/loss**, **expectancy per trade**, **total realized P/L**.
Long calls get their own spotlight panel with written insights (e.g., whether
the profit factor is being driven by exit discipline or entry timing).

## Notes & limits

- Prices come from Yahoo Finance via `yfinance`: delayed, best-effort mid
  (bid/ask midpoint, falling back to last trade). Thinly traded strikes can
  produce noisy marks.
- GitHub's cron is approximate — runs can land up to ~30+ min late. The job at
  21:20 UTC lands after the 4pm ET close in both EST and EDT.
- The scheduler pauses on repos with no activity for 60 days; any commit
  (e.g., adding a trade) revives it.
- **Not financial advice.** The signals are mechanical study aids, not
  recommendations to buy or sell anything.

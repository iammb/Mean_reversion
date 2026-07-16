# Nifty 500 Short-Side Mean Reversion — Scanner + Zerodha Kite Execution

Scans all Nifty 500 stocks for short-term overbought extremes likely to
revert toward their 20-day mean, filters them into a risk-managed trade
plan, and executes/manages the shorts through Zerodha Kite Connect.

**Everything runs DRY-RUN by default.** Going live requires a double
interlock — the `--live` flag AND `dry_run: false` in config.yaml — plus
your own API keys and a fresh daily access token.

---

## Project structure

```
mean_reversion/
├── config/
│   ├── config.yaml            # capital, risk limits, filters, entry/exit rules
│   ├── secrets.env.example    # template for Kite API keys (copy to secrets.env)
│   └── .access_token          # daily Kite token (auto-created, gitignored)
├── data/                      # Nifty 500 list, F&O lot sizes, price cache
├── scan_results/              # dated scanner output CSVs
├── orders/                    # trade plans + positions_state.json (gitignored)
├── logs/                      # dated run logs (gitignored)
├── src/mr_short/
│   ├── indicators.py          # RSI / ATR / ADX / streaks (plain pandas)
│   ├── universe.py            # constituents, lot sizes, ban list, price data
│   ├── scanner.py             # the strategy: gates, scoring, setup labels
│   ├── filters.py             # scan CSV -> tradeable shortlist (with reasons)
│   ├── risk.py                # position sizing + portfolio guardrails
│   └── kite/
│       ├── auth.py            # daily login / session
│       ├── instruments.py     # equity + nearest-expiry futures resolution
│       └── orders.py          # entry, stop/target OCO, square-off (dry-run aware)
├── scripts/                   # the daily workflow, in order:
│   ├── run_scan.py            #   1. post-close: scan -> candidates CSV
│   ├── plan_trades.py         #   2. evening: filter + size -> trade plan JSON
│   ├── kite_login.py          #   3. morning: mint today's access token
│   ├── place_entries.py       #   4. at open: place entry orders
│   └── manage_positions.py    #   5. intraday/daily: fills, exits, time stop
└── tests/                     # pytest suite: indicators, sizing, filters
```

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
# for live trading only:
cp config/secrets.env.example config/secrets.env   # add your Kite Connect keys
```

## Daily workflow

```bash
# 1. after market close - scan the universe
.venv/bin/python scripts/run_scan.py

# 2. build the trade plan (filters + position sizing)
.venv/bin/python scripts/plan_trades.py

# 3. next morning (live only) - mint today's Kite access token
.venv/bin/python scripts/kite_login.py

# 4. place entries (dry run prints orders; --live sends them)
.venv/bin/python scripts/place_entries.py           # dry run
.venv/bin/python scripts/place_entries.py --live    # real orders

# 5. run every few minutes while market is open (cron/loop)
.venv/bin/python scripts/manage_positions.py --live
```

---

## The strategy: "Fade the Exhausted Bounce"

Indian equities carry persistent upward drift (SIP/retail flows), so the
system **shorts weakness that has bounced, never strength**.

### Scanner gates (all must hold)

- ≥ 220 daily bars, close ≥ ₹50, 20-day avg turnover ≥ ₹10 crore
- **RSI(2) ≥ 85** plus at least **2 of 4**: close above upper Bollinger
  (20,2) · 20-day z-score ≥ 2 · RSI(14) ≥ 65 · ≥ 2 ATRs above the 20-EMA

### Setup labels

| Setup | Definition |
|---|---|
| **DEAD-CAT BOUNCE** (primary) | Below the 200-DMA (bonus if falling): counter-trend rally in a weak stock |
| **BLOWOFF EXTENSION** | z ≥ 2.25, or above upper band with RSI(2) ≥ 95: parabolic stretch |
| OVERBOUGHT (weak ctx) | Passes gates but lacks context — filtered out of the trade plan |

### Composite score (0–100)

Points for RSI(2)/RSI(14) extremes, upper-band close, z-score, ATR stretch,
3+/5+ up days, 5-day rally ≥ 8%/15%, volume climax. **+15** max for weak
long-term trend. Penalties: **−12** ADX ≥ 35 with +DI dominant, **−8**
strong uptrend, **−4** near 52-week high.

### Trade-plan filters (`filters.py` — every rejection logged with a reason)

score ≥ 50 · setup must be DEAD-CAT BOUNCE or BLOWOFF EXTENSION · **F&O
only** (cash shorts are intraday-only in India) · **not in the day's NSE F&O
ban list** (fetched live) · ADX ≤ 35 · reward:risk ≥ 2 · stop ≤ 3.5% away.
The daily cap (3 new trades) is applied **after sizing**, so a candidate
rejected by the sizer frees its slot for the next-ranked name. Planning also
reads the live state journal: symbols already open are skipped, and open
positions count against `max_positions` and the total-exposure cap. Plans
refuse to build from a stale scan CSV (override with `--allow-stale`).

---

## Risk management (`risk.py`)

| Guardrail | Default | Meaning |
|---|---|---|
| `risk_per_trade_pct` | 0.75% | Capital at risk between entry and stop |
| Sizing basis | breakdown trigger | Size uses the **actual fill price** (trigger below scan-day low), not the scan close — and re-checks R:R there, killing trades whose target is too close to the trigger |
| `lot_risk_tolerance` | 1.5× | Futures: 1 lot allowed only if its risk ≤ 1.5× budget, else skip |
| `max_exposure_per_trade_pct` | 25% | Notional cap per trade |
| `max_total_exposure_pct` | 60% | Total short notional cap |
| `max_positions` / `max_new_trades_per_day` | 5 / 3 | Concurrency throttles |
| `max_daily_loss_pct` | 2% | **Kill switch**: no new entries past this realised day loss |
| Margin check | live only | `order_margins` vs available balance before every entry; blocks if unsure |

With ₹10L capital the sizer will (correctly) skip most stock futures — one
lot's risk breaches the budget. Either raise capital, or set
`entry.product: MIS_EQ` for share-level intraday sizing.

## Entry & exit rules (`config.yaml`)

**Entry** (`breakdown`, default): SL SELL with trigger 0.10% below the
scan-day low, limit 0.25% below trigger. The short only triggers if the
bounce actually fails — no fill, no trade. Entry orders are DAY validity
and are re-armed each morning until `entry_valid_sessions` weekday sessions
pass, then cancelled. (`at_open` sells at market instead.)

**Exits**, placed automatically once the entry fills:
- **NRML futures (default)**: a **server-side GTT OCO** — stop leg (BUY
  trigger at swing-high + 0.25×ATR, limit with a 0.5% fill buffer) and
  target leg (BUY LIMIT at the 20-EMA). The broker cancels the sibling leg
  when one triggers, so there is no client-side race window and exits
  survive your machine being off. `manage_positions.py` verifies a
  triggered GTT actually flattened the position and falls back to a
  regular exit pair (loudly) if the limit leg didn't fill.
- **MIS equity**: regular SL-M stop + LIMIT target pair with client-side
  OCO — run the manager every few minutes intraday. Forced square-off by
  15:10.
- **Time stop**: market close-out after 7 weekday sessions — thesis
  expired. Session counters only advance on weekdays.

State machine per trade in `orders/positions_state.json`:
`PENDING_ENTRY → OPEN → CLOSED` (exit_reason: STOP / TARGET / TIME_STOP /
EOD_SQUAREOFF), or `CANCELLED` if the entry never triggers. Finished trades
are archived to `orders/trades_archive.json` — your trade journal.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

## Products

| `entry.product` | Instrument | Holding | Notes |
|---|---|---|---|
| `NRML_FUT` (default) | Nearest-expiry stock future (NFO) | Multi-day, fits the 7-session time stop | Auto-rolls to next month if ≤ 2 days to expiry; lot-size aware |
| `MIS_EQ` | Cash equity intraday | Same day only | For smaller accounts; SEBI bars overnight cash shorts |

## Data refresh (monthly)

```bash
curl -s -A "Mozilla/5.0" "https://archives.nseindia.com/content/indices/ind_nifty500list.csv" -o data/nifty500_constituents.csv
curl -s -A "Mozilla/5.0" "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv" -o data/fo_mktlots.csv
```

The F&O **ban list** is fetched live on every plan run. Skip names with
results due in the next 2 sessions — earnings gaps ignore mean reversion
(check manually; not automated).

---

*Not investment advice. Shorting carries unlimited-loss risk. Backtest and
paper-trade (dry-run mode) before pointing this at a funded account.*

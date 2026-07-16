# Nifty 500 Short-Side Mean Reversion Scanner

Scans all Nifty 500 stocks for short-term overbought extremes that are
statistically likely to revert back down toward their 20-day mean, and ranks
them as short candidates.

## Strategy: "Fade the Exhausted Bounce"

Mean reversion on the short side works differently in India than in most
markets: Indian equities carry a persistent upward drift (SIP/retail flows),
so blindly shorting strength gets run over. This scanner therefore favours
**shorting weakness that has bounced**, and penalises strength.

### Setup types

| Setup | Definition | Character |
|---|---|---|
| **DEAD-CAT BOUNCE** (primary) | Stock **below its 200-DMA** (bonus if the 200-DMA is falling) that has rallied hard into short-term overbought | Highest-probability short: counter-trend rally in a weak stock, reverting with the larger trend |
| **BLOWOFF EXTENSION** | Stock stretched to a statistical extreme above its 20-day mean (z-score ≥ 2.25, or above the upper Bollinger with RSI(2) ≥ 95) | Pure reversion trade against a parabolic move; needs stricter thresholds |
| **OVERBOUGHT (weak ctx)** | Passes the gate but lacks either context | Lower conviction; trade smaller or skip |

### Eligibility gate (all must hold)

- ≥ 220 daily bars of history, close ≥ ₹50
- 20-day average daily turnover ≥ ₹10 crore (`--min-turnover` to change)
- **RSI(2) ≥ 85** — the stock must be genuinely short-term overbought
- At least **2 of 4** confirmations: %B ≥ 1.0 (close above upper Bollinger
  20,2) · 20-day z-score ≥ 2 · RSI(14) ≥ 65 · ATR-stretch ≥ 2
  (close is ≥ 2 ATRs above the 20-EMA)

### Composite score (0–100)

Points for: RSI(2) ≥ 90/97 · RSI(14) ≥ 65/72 · close above upper Bollinger ·
z-score ≥ 2/2.5 · ATR-stretch ≥ 2/3 · 3+/5+ consecutive up days · 5-day rally
≥ 8%/15% · volume climax (≥ 2× 20-day avg volume on an up day).

Context: **+10** below 200-DMA, **+5** falling 200-DMA.
Penalties (don't fade freight trains): **−12** if ADX(14) ≥ 35 with +DI > −DI,
**−8** if in a strong uptrend (above rising 200-DMA and above 50-DMA),
**−4** within 2% of the 52-week high (breakout/squeeze risk).

### Trade plan (emitted per candidate)

- **Entry**: at scan-day close, or (safer) next day only on a break below the
  scan-day low — confirmation that the bounce is failing.
- **Stop**: 5-day swing high + 0.25 × ATR(14).
- **Target**: the 20-EMA (that *is* the mean being reverted to).
- **Time stop**: exit after 7 sessions regardless — if reversion hasn't
  happened in a week, the thesis is wrong.
- Size so a stop-out costs ≤ 0.5–1% of capital. Reward:risk is printed;
  prefer ≥ 2.

## India-specific rules baked in

1. **Cash-market shorts are intraday-only** (SEBI). To hold a short
   overnight you need the stock in the F&O segment (short futures or buy
   puts). The `fno` column flags this; `--fno-only` filters to those.
2. **Check the F&O ban list** for the day before trading a flagged name.
3. **Skip earnings**: don't short within 2 sessions of a results date
   (not automated — check before entering).
4. Liquidity/price gates avoid circuit-prone smallcaps where borrows and
   fills don't exist.

## Usage

```bash
.venv/bin/python scanner.py                # full scan, top 20
.venv/bin/python scanner.py --top 40       # more rows
.venv/bin/python scanner.py --fno-only     # only overnight-shortable names
.venv/bin/python scanner.py --min-turnover 25   # stricter liquidity
.venv/bin/python scanner.py --cache        # reuse today's downloaded prices
```

Output: ranked table + full CSV in `scan_results/short_candidates_<date>.csv`.

Data: Yahoo Finance daily OHLCV (`yfinance`, auto-adjusted). Universe:
`data/nifty500_constituents.csv` (NSE archives). F&O list:
`data/fo_mktlots.csv` (NSE archives) — refresh both monthly:

```bash
curl -s -A "Mozilla/5.0" "https://archives.nseindia.com/content/indices/ind_nifty500list.csv" -o data/nifty500_constituents.csv
curl -s -A "Mozilla/5.0" "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv" -o data/fo_mktlots.csv
```

*Not investment advice. Shorting carries unlimited-loss risk; backtest and
paper-trade before deploying capital.*

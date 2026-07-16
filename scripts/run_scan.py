#!/usr/bin/env python3
"""Step 1 (post-close): scan Nifty 500, write ranked short candidates CSV."""

import argparse

from mr_short.scanner import DEFAULT_MIN_TURNOVER_CR, run_scan


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--top", type=int, default=20, help="rows to print")
    ap.add_argument("--min-turnover", type=float, default=DEFAULT_MIN_TURNOVER_CR,
                    help="min 20d avg daily turnover, Rs crore")
    ap.add_argument("--cache", action="store_true", help="reuse prices if <20h old")
    args = ap.parse_args()

    res = run_scan(min_turnover_cr=args.min_turnover, use_cache=args.cache)
    if res.empty:
        return
    cols = ["symbol", "score", "setup", "close", "rsi2", "zscore", "atr_stretch",
            "adx", "below_200dma", "fno", "entry", "stop", "target_20ema", "reward_risk"]
    print(res[cols].head(args.top).to_string())


if __name__ == "__main__":
    main()

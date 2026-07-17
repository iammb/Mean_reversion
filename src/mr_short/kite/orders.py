"""Order engine: entries, exits (GTT OCO or regular pair), square-off.

Every method works in two modes:
  dry_run=True  (default) - logs exactly what WOULD be placed, returns fake
                            order ids "DRY-n" / "DRYGTT-n". Nothing touches
                            the broker.
  dry_run=False (--live)  - places real orders via Kite Connect.

Entry (short):
  breakdown mode - SL SELL: trigger just below scan-day low, limit a bit
                   lower. Fills only if the bounce actually fails.
  at_open mode   - MARKET SELL at next open.

Exits after fill:
  NRML futures (default) - a server-side GTT OCO: stop leg above, target leg
    below; the broker cancels the sibling when one triggers. Survives this
    machine being off and has no client-side race window. GTT legs fire
    LIMIT orders, so the stop leg's limit carries a fill buffer above its
    trigger; manage_positions verifies the position actually closed.
  MIS equity - regular SL-M stop + LIMIT target pair, OCO enforced by
    manage_positions (run it every few minutes intraday).
  A time stop closes any position at market after `time_stop_sessions`.
"""

import itertools

from ..utils import get_logger, inr
from .instruments import Instrument

log = get_logger("kite.orders")

_dry_counter = itertools.count(1)


def _round_tick(px: float, tick: float = 0.05) -> float:
    return round(round(px / tick) * tick, 2)


class OrderEngine:
    def __init__(self, kite, cfg: dict, dry_run: bool = True):
        self.kite = kite
        self.cfg = cfg
        self.dry_run = dry_run or kite is None
        self.tag = cfg["kite"]["order_tag"]

    # ------------------------------------------------------------------
    def _place(self, desc: str, **params) -> str:
        if self.dry_run:
            oid = f"DRY-{next(_dry_counter)}"
            log.info(f"[DRY RUN] {desc} :: {params}")
            return oid
        oid = self.kite.place_order(variety=self.kite.VARIETY_REGULAR, tag=self.tag, **params)
        log.info(f"[LIVE] {desc} -> order_id {oid}")
        return oid

    def _product_code(self, inst: Instrument) -> str:
        if inst.exchange == "NFO":
            return "NRML"
        return "MIS"

    # ------------------------------------------------------------------
    def place_short_entry(self, inst: Instrument, qty: int, scan_day_low: float) -> str:
        e = self.cfg["entry"]
        if e["mode"] == "breakdown":
            trigger = _round_tick(scan_day_low * (1 - e["breakdown_buffer_pct"] / 100.0))
            limit = _round_tick(trigger * (1 - e["limit_slippage_pct"] / 100.0))
            return self._place(
                f"ENTRY short {inst.tradingsymbol} x{qty} SL-SELL trig {trigger} lim {limit}",
                exchange=inst.exchange, tradingsymbol=inst.tradingsymbol,
                transaction_type="SELL", quantity=qty,
                product=self._product_code(inst), order_type="SL",
                price=limit, trigger_price=trigger, validity="DAY",
            )
        # at_open
        return self._place(
            f"ENTRY short {inst.tradingsymbol} x{qty} MARKET",
            exchange=inst.exchange, tradingsymbol=inst.tradingsymbol,
            transaction_type="SELL", quantity=qty,
            product=self._product_code(inst), order_type="MARKET", validity="DAY",
        )

    def place_short_market(self, inst: Instrument, qty: int, reason: str) -> str:
        """Immediate short entry - used by the live 1-minute confirmation."""
        return self._place(
            f"ENTRY short {inst.tradingsymbol} x{qty} MARKET ({reason})",
            exchange=inst.exchange, tradingsymbol=inst.tradingsymbol,
            transaction_type="SELL", quantity=qty,
            product=self._product_code(inst), order_type="MARKET", validity="DAY",
        )

    # ------------------------------------------------------------------
    def place_exit_pair(self, inst: Instrument, qty: int, stop: float, target: float):
        """Stop-loss + target for an open short. Returns (sl_id, target_id)."""
        sl_id = self._place(
            f"STOP {inst.tradingsymbol} x{qty} SL-M BUY trig {_round_tick(stop)}",
            exchange=inst.exchange, tradingsymbol=inst.tradingsymbol,
            transaction_type="BUY", quantity=qty,
            product=self._product_code(inst), order_type="SL-M",
            trigger_price=_round_tick(stop), validity="DAY",
        )
        tgt_id = self._place(
            f"TARGET {inst.tradingsymbol} x{qty} LIMIT BUY {_round_tick(target)}",
            exchange=inst.exchange, tradingsymbol=inst.tradingsymbol,
            transaction_type="BUY", quantity=qty,
            product=self._product_code(inst), order_type="LIMIT",
            price=_round_tick(target), validity="DAY",
        )
        return sl_id, tgt_id

    # ------------------------------------------------------------------
    # GTT OCO exits (NRML): broker-side, one leg cancels the other
    # ------------------------------------------------------------------
    def place_gtt_oco(self, inst: Instrument, qty: int, stop: float,
                      target: float, last_price: float) -> str:
        """Two-leg GTT for an open short: leg 0 = target (below), leg 1 = stop
        (above). Returns the GTT trigger id."""
        buffer_pct = self.cfg["exits"].get("gtt_stop_limit_buffer_pct", 0.5)
        tgt_px = _round_tick(target)
        stop_trig = _round_tick(stop)
        stop_limit = _round_tick(stop * (1 + buffer_pct / 100.0))  # BUY limit above trigger -> fills
        desc = (f"GTT-OCO {inst.tradingsymbol} x{qty}: target BUY {tgt_px} / "
                f"stop BUY trig {stop_trig} lim {stop_limit}")
        if self.dry_run:
            gid = f"DRYGTT-{next(_dry_counter)}"
            log.info(f"[DRY RUN] {desc}")
            return gid
        legs = [
            {"transaction_type": "BUY", "quantity": qty, "product": "NRML",
             "order_type": "LIMIT", "price": tgt_px},
            {"transaction_type": "BUY", "quantity": qty, "product": "NRML",
             "order_type": "LIMIT", "price": stop_limit},
        ]
        result = self.kite.place_gtt(
            trigger_type=self.kite.GTT_TYPE_OCO,
            tradingsymbol=inst.tradingsymbol, exchange=inst.exchange,
            trigger_values=[tgt_px, stop_trig], last_price=last_price,
            orders=legs,
        )
        gid = str(result["trigger_id"])
        log.info(f"[LIVE] {desc} -> gtt_id {gid}")
        return gid

    def gtt_status(self, gtt_id: str) -> str:
        """active | triggered | cancelled | deleted | disabled | expired | rejected."""
        if self.dry_run or str(gtt_id).startswith("DRYGTT-"):
            return "active"
        try:
            return self.kite.get_gtt(int(gtt_id))["status"]
        except Exception as e:
            log.warning(f"gtt_status({gtt_id}): {e}")
            return "UNKNOWN"

    def gtt_triggered_leg(self, gtt_id: str) -> str:
        """Which leg fired on a triggered GTT: TARGET (leg 0) or STOP (leg 1)."""
        if self.dry_run or str(gtt_id).startswith("DRYGTT-"):
            return "TARGET"
        try:
            g = self.kite.get_gtt(int(gtt_id))
            for idx, leg in enumerate(g.get("orders", [])):
                if leg.get("result"):
                    return "TARGET" if idx == 0 else "STOP"
        except Exception as e:
            log.warning(f"gtt_triggered_leg({gtt_id}): {e}")
        return "GTT_TRIGGERED"

    def delete_gtt(self, gtt_id: str, why: str):
        if self.dry_run or str(gtt_id).startswith("DRYGTT-"):
            log.info(f"[DRY RUN] DELETE GTT {gtt_id} ({why})")
            return
        self.kite.delete_gtt(int(gtt_id))
        log.info(f"[LIVE] DELETE GTT {gtt_id} ({why})")

    # ------------------------------------------------------------------
    def ltp(self, inst: Instrument, fallback: float) -> float:
        if self.dry_run:
            return fallback
        try:
            key = f"{inst.exchange}:{inst.tradingsymbol}"
            return self.kite.ltp([key])[key]["last_price"]
        except Exception as e:
            log.warning(f"ltp({inst.tradingsymbol}): {e} - using fallback {fallback}")
            return fallback

    def net_position_qty(self, inst: Instrument) -> int:
        """Net quantity for this instrument (negative = short). Dry run: 0 (flat)."""
        if self.dry_run:
            return 0
        try:
            for p in self.kite.positions()["net"]:
                if p["tradingsymbol"] == inst.tradingsymbol and p["exchange"] == inst.exchange:
                    return int(p["quantity"])
        except Exception as e:
            log.warning(f"net_position_qty({inst.tradingsymbol}): {e}")
        return 0

    # ------------------------------------------------------------------
    def close_at_market(self, inst: Instrument, qty: int, reason: str) -> str:
        return self._place(
            f"CLOSE {inst.tradingsymbol} x{qty} MARKET BUY ({reason})",
            exchange=inst.exchange, tradingsymbol=inst.tradingsymbol,
            transaction_type="BUY", quantity=qty,
            product=self._product_code(inst), order_type="MARKET", validity="DAY",
        )

    def flatten_accidental_long(self, inst: Instrument, qty: int) -> str:
        """This is a SHORT-ONLY system. If both exit legs of a pair ever fill
        (OCO race) the account would be net long - dump it immediately."""
        return self._place(
            f"FLATTEN ACCIDENTAL LONG {inst.tradingsymbol} x{qty} MARKET SELL",
            exchange=inst.exchange, tradingsymbol=inst.tradingsymbol,
            transaction_type="SELL", quantity=qty,
            product=self._product_code(inst), order_type="MARKET", validity="DAY",
        )

    def cancel(self, order_id: str, why: str):
        if self.dry_run or str(order_id).startswith("DRY-"):
            log.info(f"[DRY RUN] CANCEL {order_id} ({why})")
            return
        self.kite.cancel_order(variety=self.kite.VARIETY_REGULAR, order_id=order_id)
        log.info(f"[LIVE] CANCEL {order_id} ({why})")

    # ------------------------------------------------------------------
    def order_status(self, order_id: str) -> str:
        """COMPLETE | OPEN | TRIGGER PENDING | CANCELLED | REJECTED | UNKNOWN."""
        if self.dry_run or str(order_id).startswith("DRY-"):
            return "OPEN"
        try:
            hist = self.kite.order_history(order_id)
            return hist[-1]["status"] if hist else "UNKNOWN"
        except Exception as e:
            log.warning(f"order_status({order_id}): {e}")
            return "UNKNOWN"

    def check_margin(self, inst: Instrument, qty: int, price: float) -> bool:
        """Best-effort margin sanity check before a live entry."""
        if self.dry_run:
            return True
        try:
            need = self.kite.order_margins([{
                "exchange": inst.exchange, "tradingsymbol": inst.tradingsymbol,
                "transaction_type": "SELL", "variety": "regular",
                "product": self._product_code(inst), "order_type": "MARKET",
                "quantity": qty, "price": price,
            }])[0]["total"]
            avail = self.kite.margins()["equity"]["available"]["live_balance"]
            if need > avail:
                log.error(f"insufficient margin for {inst.tradingsymbol}: "
                          f"need {inr(need)}, available {inr(avail)}")
                return False
            log.info(f"margin ok for {inst.tradingsymbol}: need {inr(need)}, "
                     f"available {inr(avail)}")
            return True
        except Exception as e:
            log.warning(f"margin check failed ({e}) - blocking entry to be safe")
            return False

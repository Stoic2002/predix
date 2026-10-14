"""Engine backtest bar-per-bar (harian) dengan ekuitas bersama untuk satu atau beberapa sleeve.

Sleeve = satu strategi pada satu simbol. Strategi hanya menyerahkan DataFrame sinyal
(kolom SIGNAL_COLS) yang dihitung dari candle yang sudah close; engine yang mengurus
eksekusi, biaya, sizing, dan saldo.

Urutan di setiap bar t:
  1. Open     : eksekusi exit tertunda, lalu entry tertunda. Sizing memakai ekuitas close t-1.
  2. Intrabar : funding event yang jatuh di bar t, lalu cek stop (low/high menyentuh stop).
  3. Close    : mark-to-market ekuitas, lalu baca sinyal bar t untuk dieksekusi di open t+1.

Asumsi eksekusi (dipilih versi yang lebih merugikan kalau ragu):
  - Semua fill, termasuk stop, kena slippage dan fee taker.
  - Stop diisi di harga stop; kalau open sudah melewati stop (gap), diisi di open.
  - Stop yang kena di bar t mengalahkan exit sinyal dari close bar t.
  - Stop = harga fill entry ∓ stop_dist; stop tetap (tidak trailing).
  - Funding dihitung dengan notional = qty × open bar tempat event jatuh. Posisi yang masuk
    di open bar t ikut membayar/menerima semua event bar t; posisi yang keluar di open bar t
    tidak. Di bar saat stop kena (waktu pastinya tidak diketahui), hanya event yang
    membuat posisi membayar yang dihitung.
  - Sinyal berlawanan saat posisi terbuka: tutup di open t+1, buka posisi berlawanan di
    open t+2 tanpa cek ulang sinyal, memakai stop_dist dari bar sinyal t.
  - Posisi yang masih terbuka di bar terakhir periode ditutup di close bar itu ("end").
  - Risiko per trade = risk_per_trade ÷ jumlah sleeve (ekuitas bersama, dibagi rata).

Engine adalah state machine yang memproses satu bar per panggilan dan bisa disimpan/dilanjutkan
(to_state/from_state). run_backtest dan bot paper trading memakai kelas yang sama, sehingga
aturan eksekusi di keduanya identik.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, fields

import numpy as np
import pandas as pd

from src import costs, sizing

# entry: +1 long / -1 short / 0 tidak ada. stop_dist: jarak harga ke stop (mis. 3 × ATR).
SIGNAL_COLS = ["entry", "exit_long", "exit_short", "stop_dist"]


@dataclass(frozen=True)
class BacktestConfig:
    initial_equity: float = 10_000.0
    fee_taker: float = 0.0005
    slippage: float = 0.0005
    risk_per_trade: float = 0.01
    max_leverage_per_position: float = 2.0
    max_total_notional: float = 3.0
    isolated_leverage: float = 3.0
    maintenance_margin_rate: float = 0.005
    liq_buffer: float = 1.5
    # Minimal nilai order per simbol (mis. aturan exchange). None = tidak ada batas (default robot paper).
    min_notional: dict | None = None

    @classmethod
    def from_config(cls, cfg: dict) -> BacktestConfig:
        """Ambil nilai dari bagian backtest/costs/risk di config.yaml."""
        flat = {**cfg.get("backtest", {}), **cfg.get("costs", {}), **cfg.get("risk", {})}
        names = {f.name for f in fields(cls)} - {"min_notional"}
        return cls(**{k: float(v) for k, v in flat.items() if k in names})


@dataclass
class Sleeve:
    name: str
    symbol: str
    signals: pd.DataFrame
    max_hold_bars: int | None = None  # time stop; None = tidak ada


@dataclass
class BacktestResult:
    equity: pd.DataFrame      # per bar: equity, cash, notional, n_open
    sleeve_pnl: pd.DataFrame  # per bar: PnL kumulatif bersih tiap sleeve (realized + unrealized)
    trades: pd.DataFrame
    rejections: pd.DataFrame
    config: BacktestConfig
    risk_per_trade: float     # risiko per trade efektif per sleeve


@dataclass
class _Position:
    direction: int
    qty: float
    entry_price: float
    entry_ref: float
    stop: float
    risk_amount: float
    entry_i: int
    entry_time: pd.Timestamp
    signal_time: pd.Timestamp
    capped_by: str | None
    fees: float = 0.0
    funding: float = 0.0
    slippage: float = 0.0

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["entry_time"], d["signal_time"] = self.entry_time.isoformat(), self.signal_time.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> _Position:
        return cls(**{**d, "entry_time": pd.Timestamp(d["entry_time"]),
                      "signal_time": pd.Timestamp(d["signal_time"])})


def _utc(x) -> pd.Timestamp:
    ts = pd.Timestamp(x)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _side(direction: int) -> str:
    return "long" if direction > 0 else "short"


class Engine:
    """State machine bar-per-bar. Satu panggilan process_bar = satu candle yang sudah close."""

    def __init__(self, sleeves: list[Sleeve], cfg: BacktestConfig):
        if not sleeves:
            raise ValueError("minimal satu sleeve")
        self.names = [s.name for s in sleeves]
        if len(set(self.names)) != len(self.names):
            raise ValueError("nama sleeve harus unik")
        self.sleeves = sleeves
        self.cfg = cfg
        self.risk_frac = cfg.risk_per_trade / len(sleeves)
        self.i = -1                       # nomor bar terakhir yang diproses
        self.cash = cfg.initial_equity
        self.equity_prev = cfg.initial_equity
        self.entries_blocked = False      # kill switch: tidak ada entry baru selama True
        self.pos: dict[str, _Position | None] = dict.fromkeys(self.names)
        self.pending_exit: dict[str, str | None] = dict.fromkeys(self.names)
        self.pending_entry: dict[str, tuple | None] = dict.fromkeys(self.names)
        self.queued_reversal: dict[str, tuple | None] = dict.fromkeys(self.names)
        self.realized = dict.fromkeys(self.names, 0.0)
        # Keluaran yang dikumpulkan; pemanggil boleh mengosongkannya setelah dibaca.
        self.trades: list[dict] = []
        self.rejections: list[dict] = []
        self.fills: list[dict] = []
        self.last_row: tuple | None = None          # (time, equity, cash, notional, n_open)
        self.last_sleeve_pnl: list[float] | None = None

    # ------------------------------------------------------------ simpan / lanjutkan

    def to_state(self) -> dict:
        def pend(v):
            return None if v is None else [v[0], v[1], v[2].isoformat()]
        return {
            "names": self.names, "i": self.i, "cash": self.cash, "equity_prev": self.equity_prev,
            "entries_blocked": self.entries_blocked, "realized": self.realized,
            "pos": {n: (p.to_dict() if p else None) for n, p in self.pos.items()},
            "pending_exit": self.pending_exit,
            "pending_entry": {n: pend(v) for n, v in self.pending_entry.items()},
            "queued_reversal": {n: pend(v) for n, v in self.queued_reversal.items()},
        }

    @classmethod
    def from_state(cls, sleeves: list[Sleeve], cfg: BacktestConfig, state: dict) -> Engine:
        eng = cls(sleeves, cfg)
        if state["names"] != eng.names:
            raise ValueError(f"sleeve tersimpan {state['names']} beda dengan {eng.names}")

        def pend(v):
            return None if v is None else (int(v[0]), float(v[1]), pd.Timestamp(v[2]))
        eng.i, eng.cash, eng.equity_prev = state["i"], state["cash"], state["equity_prev"]
        eng.entries_blocked = state["entries_blocked"]
        eng.realized = dict(state["realized"])
        eng.pos = {n: (_Position.from_dict(p) if p else None) for n, p in state["pos"].items()}
        eng.pending_exit = dict(state["pending_exit"])
        eng.pending_entry = {n: pend(v) for n, v in state["pending_entry"].items()}
        eng.queued_reversal = {n: pend(v) for n, v in state["queued_reversal"].items()}
        return eng

    # ------------------------------------------------------------ inti

    def _close(self, s: Sleeve, t: pd.Timestamp, ref: float, reason: str) -> None:
        cfg, p = self.cfg, self.pos[s.name]
        fill = costs.apply_slippage(ref, -p.direction, cfg.slippage)
        fee = costs.fee(p.qty * fill, cfg.fee_taker)
        gross = p.direction * p.qty * (fill - p.entry_price)
        self.cash += gross - fee
        self.realized[s.name] += gross - fee
        p.fees += fee
        p.slippage += p.qty * abs(fill - ref)
        intrabar = reason in ("stop", "end")
        net = gross - p.fees - p.funding
        self.fills.append({"time": t, "sleeve": s.name, "symbol": s.symbol, "action": "exit",
                           "side": _side(p.direction), "reason": reason, "ref_price": ref,
                           "fill_price": fill, "qty": p.qty, "fee": fee})
        self.trades.append({
            "sleeve": s.name, "symbol": s.symbol, "side": _side(p.direction),
            "signal_time": p.signal_time, "entry_time": p.entry_time, "exit_time": t,
            "entry_ref": p.entry_ref, "entry_price": p.entry_price, "stop": p.stop,
            "exit_ref": ref, "exit_price": fill, "exit_reason": reason,
            "qty": p.qty, "notional": p.qty * p.entry_price, "risk_amount": p.risk_amount,
            "capped_by": p.capped_by,
            "bars_held": self.i - p.entry_i + (1 if intrabar else 0),
            "gross_pnl": gross, "fees": p.fees, "funding": p.funding, "slippage": p.slippage,
            "net_pnl": net, "r_multiple": net / p.risk_amount,
        })
        self.pos[s.name] = None

    def process_bar(self, t: pd.Timestamp, prices: dict[str, tuple], funding: dict[str, list],
                    signals: dict[str, tuple] | None, close_all: bool = False) -> None:
        """prices[symbol] = (open, high, low, close), hanya untuk simbol yang sudah listing di bar ini;
        funding[symbol] = rate event di bar ini;
        signals[sleeve] = (entry, exit_long, exit_short, stop_dist) dari close bar ini.
        close_all=True menutup semua posisi di close (akhir periode backtest) tanpa membaca sinyal."""
        self.i += 1
        i, cfg = self.i, self.cfg

        # 1a. Exit tertunda di open.
        for s in self.sleeves:
            if self.pending_exit[s.name] and self.pos[s.name] is not None:
                self._close(s, t, prices[s.symbol][0], self.pending_exit[s.name])
            self.pending_exit[s.name] = None

        # 1b. Entry tertunda di open, sizing dengan ekuitas close sebelumnya.
        open_notional = sum(p.qty * prices[s.symbol][0]
                            for s in self.sleeves if (p := self.pos[s.name]) is not None)
        for s in self.sleeves:
            pe, self.pending_entry[s.name] = self.pending_entry[s.name], None
            if pe is None or self.pos[s.name] is not None:
                continue
            direction, stop_dist, signal_time = pe
            ref = prices[s.symbol][0]
            reject = {"time": t, "sleeve": s.name, "symbol": s.symbol, "side": _side(direction),
                      "signal_time": signal_time, "stop_dist": stop_dist}
            if self.entries_blocked:
                self.rejections.append({**reject, "liq_price": math.nan, "reason": "kill switch aktif"})
                continue
            fill = costs.apply_slippage(ref, direction, cfg.slippage)
            res = sizing.size_position(
                equity=self.equity_prev, entry_price=fill, stop_dist=stop_dist, direction=direction,
                risk_frac=self.risk_frac, max_leverage=cfg.max_leverage_per_position,
                max_total_notional=cfg.max_total_notional, open_notional=open_notional,
                isolated_leverage=cfg.isolated_leverage,
                maintenance_margin_rate=cfg.maintenance_margin_rate, liq_buffer=cfg.liq_buffer,
            )
            if not res.rejected and cfg.min_notional and res.notional < cfg.min_notional.get(s.symbol, 0.0):
                res = sizing.SizeResult(liq_price=res.liq_price, rejected=(
                    f"di bawah minimal order: {res.notional:.2f} < {cfg.min_notional[s.symbol]:.2f}"))
            if res.rejected:
                self.rejections.append({**reject, "liq_price": res.liq_price, "reason": res.rejected})
                continue
            fee = costs.fee(res.notional, cfg.fee_taker)
            self.cash -= fee
            self.realized[s.name] -= fee
            self.pos[s.name] = _Position(
                direction=direction, qty=res.qty, entry_price=fill, entry_ref=ref,
                stop=fill - direction * stop_dist, risk_amount=res.risk_amount,
                entry_i=i, entry_time=t, signal_time=signal_time, capped_by=res.capped_by,
                fees=fee, slippage=res.qty * abs(fill - ref),
            )
            self.fills.append({"time": t, "sleeve": s.name, "symbol": s.symbol, "action": "entry",
                               "side": _side(direction), "reason": "signal", "ref_price": ref,
                               "fill_price": fill, "qty": res.qty, "fee": fee})
            open_notional += res.notional

        # 2. Intrabar: funding lalu stop.
        for s in self.sleeves:
            p = self.pos[s.name]
            if p is None:
                continue
            o, h, l, _ = prices[s.symbol]
            hit = l <= p.stop if p.direction > 0 else h >= p.stop
            for rate in funding.get(s.symbol, ()):
                pay = costs.funding_payment(p.direction, p.qty, o, rate)
                if hit and pay < 0:
                    continue
                self.cash -= pay
                self.realized[s.name] -= pay
                p.funding += pay
            if hit:
                ref = min(o, p.stop) if p.direction > 0 else max(o, p.stop)
                self._close(s, t, ref, "stop")

        # 3. Close: (opsional) tutup semua, lalu mark-to-market.
        if close_all:
            for s in self.sleeves:
                if self.pos[s.name] is not None:
                    self._close(s, t, prices[s.symbol][3], "end")
        unreal, notional = {}, 0.0
        for s in self.sleeves:
            p = self.pos[s.name]
            if p is None:
                unreal[s.name] = 0.0
                continue
            c = prices[s.symbol][3]
            unreal[s.name] = p.direction * p.qty * (c - p.entry_price)
            notional += p.qty * c
        equity = self.cash + sum(unreal.values())
        n_open = sum(p is not None for p in self.pos.values())
        self.last_row = (t, equity, self.cash, notional, n_open)
        self.last_sleeve_pnl = [self.realized[n] + unreal[n] for n in self.names]
        self.equity_prev = equity
        if close_all or signals is None:
            return

        # Sinyal close bar t -> aksi di open t+1.
        for s in self.sleeves:
            n = s.name
            if self.queued_reversal[n] is not None:
                self.pending_entry[n], self.queued_reversal[n] = self.queued_reversal[n], None
                continue
            entry, exit_long, exit_short, sd = signals[n]
            valid_sd = math.isfinite(sd) and sd > 0
            p = self.pos[n]
            if p is not None:
                d = p.direction
                if entry == -d and valid_sd:
                    self.pending_exit[n] = "reversal"
                    self.queued_reversal[n] = (-d, float(sd), t)
                elif exit_long if d > 0 else exit_short:
                    self.pending_exit[n] = "signal"
                elif s.max_hold_bars and i - p.entry_i + 1 >= s.max_hold_bars:
                    self.pending_exit[n] = "time"
            elif entry != 0 and valid_sd:
                self.pending_entry[n] = (int(entry), float(sd), t)

    # ------------------------------------------------------------ laporan status

    def open_positions(self, closes: dict[str, float]) -> list[dict]:
        out = []
        for s in self.sleeves:
            p = self.pos[s.name]
            if p is None:
                continue
            c = closes[s.symbol]
            out.append({"sleeve": s.name, "symbol": s.symbol, "side": _side(p.direction),
                        "entry_time": p.entry_time.isoformat(), "entry_price": p.entry_price,
                        "stop": p.stop, "qty": p.qty, "last_close": c,
                        "unrealized": p.direction * p.qty * (c - p.entry_price),
                        "fees": p.fees, "funding": p.funding})
        return out

    def planned_orders(self) -> list[dict]:
        """Order yang akan dieksekusi di open bar berikutnya (hasil sinyal close terakhir)."""
        out = []
        for s in self.sleeves:
            n = s.name
            if self.pending_exit[n] and self.pos[n] is not None:
                out.append({"sleeve": n, "symbol": s.symbol, "action": "exit",
                            "side": _side(self.pos[n].direction), "reason": self.pending_exit[n]})
            pe = self.pending_entry[n]
            if pe is not None:
                out.append({"sleeve": n, "symbol": s.symbol, "action": "entry", "side": _side(pe[0]),
                            "reason": "kill switch: akan ditolak" if self.entries_blocked else "signal",
                            "stop_dist": pe[1]})
            if self.queued_reversal[n] is not None:
                rv = self.queued_reversal[n]
                out.append({"sleeve": n, "symbol": s.symbol, "action": "entry (bar setelahnya)",
                            "side": _side(rv[0]), "reason": "reversal", "stop_dist": rv[1]})
        return out


def run_backtest(
    bars: dict[str, pd.DataFrame],
    funding: dict[str, pd.DataFrame],
    sleeves: list[Sleeve],
    cfg: BacktestConfig,
    start=None,
    end=None,
    bar: str = "D",
) -> BacktestResult:
    """bars[symbol]: DataFrame open/high/low/close berindeks waktu UTC. `bar` = panjang candle ("D", "4h", ...).
    funding[symbol]: DataFrame kolom funding_rate berindeks waktu UTC event.
    Sinyal sebaiknya dihitung dari seluruh histori supaya indikator sudah panas di `start`."""
    eng = Engine(sleeves, cfg)
    symbols = sorted({s.symbol for s in sleeves})
    index = bars[symbols[0]].index
    for sym in symbols[1:]:
        index = index.union(bars[sym].index)
    if start is not None:
        index = index[index >= _utc(start)]
    if end is not None:
        index = index[index <= _utc(end)]
    if len(index) == 0:
        raise ValueError("tidak ada bar di periode yang diminta")

    px: dict[str, np.ndarray] = {}
    fund: dict[str, dict[int, list]] = {}
    listed_from: dict[str, int] = {}
    for sym in symbols:
        b = bars[sym].reindex(index)[["open", "high", "low", "close"]]
        valid = b.notna().all(axis=1).to_numpy()
        if not valid.any():
            raise ValueError(f"{sym}: tidak ada data di periode backtest")
        first = int(valid.argmax())
        if not valid[first:].all():  # simbol boleh listing belakangan, tapi tidak boleh bolong
            raise ValueError(f"{sym}: ada bar hilang di dalam periode backtest")
        listed_from[sym] = first
        px[sym] = b.to_numpy(float)
        fund[sym] = funding_by_day(funding[sym], bar)

    sig = {s.name: signal_arrays(s.signals, index) for s in sleeves}
    eq_rows, pnl_rows = [], []
    last = len(index) - 1
    for i, t in enumerate(index):
        eng.process_bar(
            t,
            prices={sym: tuple(px[sym][i]) for sym in symbols if i >= listed_from[sym]},
            funding={sym: fund[sym].get(t.value, ()) for sym in symbols},
            signals={n: tuple(a[i] for a in g) for n, g in sig.items()},
            close_all=(i == last),
        )
        eq_rows.append(eng.last_row)
        pnl_rows.append(eng.last_sleeve_pnl)

    eq = pd.DataFrame(eq_rows, columns=["time", "equity", "cash", "notional", "n_open"]).set_index("time")
    return BacktestResult(
        equity=eq,
        sleeve_pnl=pd.DataFrame(pnl_rows, index=eq.index, columns=eng.names),
        trades=pd.DataFrame(eng.trades),
        rejections=pd.DataFrame(eng.rejections),
        config=cfg,
        risk_per_trade=eng.risk_frac,
    )


def funding_by_day(f: pd.DataFrame, freq: str = "D") -> dict[int, list]:
    """Kelompokkan event funding per bar (default hari UTC; mis. "4h" untuk candle 4 jam).
    Kunci: Timestamp.value awal bar tempat event jatuh."""
    return {d.value: g.tolist() for d, g in f["funding_rate"].groupby(f.index.floor(freq))}


def signal_arrays(signals: pd.DataFrame, index: pd.DatetimeIndex) -> tuple:
    missing = set(SIGNAL_COLS) - set(signals.columns)
    if missing:
        raise ValueError(f"kolom sinyal hilang {sorted(missing)}")
    g = signals.reindex(index)
    return (g["entry"].fillna(0).to_numpy().astype(int),
            g["exit_long"].eq(True).to_numpy(),
            g["exit_short"].eq(True).to_numpy(),
            g["stop_dist"].to_numpy(float))

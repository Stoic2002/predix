"""Fase 2 A1 — Paper trading (dry-run): bot harian yang memproses candle baru yang sudah close.

    python -m src.paper                      # bot utama (bagian `paper` di config)
    python -m src.paper --all                # semua bot (semua bagian `paper*` di config), berurutan
    python -m src.paper --bot paper_tsmom    # satu bot tertentu
    python -m src.paper --no-fetch           # proses dari CSV yang ada, tanpa jaringan
    python -m src.paper --reset-kill-switch  # izinkan entry lagi setelah kill switch aktif
    python -m src.paper --db state/paper_demo.db --start 2026-01-01   # DEMO: replay histori ke db terpisah

Aman dijalankan kapan saja dan berulang kali: candle yang sudah diproses dilewati, candle yang
terlewat (mis. laptop mati 3 hari) diproses berurutan. Eksekusi memakai src.backtest.Engine
yang sama dengan backtest, jadi hasil paper bisa dibandingkan langsung dengan backtest.
Mode dry-run tidak menghubungi exchange dengan akun apa pun; hanya data publik.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml

from src import notify
from src.backtest import BacktestConfig, Engine, Sleeve, _utc, funding_by_day, signal_arrays
from src.data import ROOT, connect, load_bars, load_config, load_funding, update_ohlcv, update_symbol
from src.run import asset, make_sleeve
from src.strategies.registry import REGISTRY
from src.summary import bar_delta, bot_timeframe

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS equity (time TEXT PRIMARY KEY, equity REAL, cash REAL, notional REAL,
                                   n_open INTEGER, drawdown REAL);
CREATE TABLE IF NOT EXISTS runs (run_at TEXT, bars_processed INTEGER, last_bar TEXT, note TEXT);
"""


# ---------------------------------------------------------------- setup

PANDAS_FREQ = {"1d": "D", "4h": "4h", "1h": "h"}


def bot_sections(cfg: dict) -> list[str]:
    """Semua bagian config yang mendefinisikan bot paper: nama diawali 'paper', punya strategy dan db."""
    return [k for k, v in cfg.items()
            if k.startswith("paper") and isinstance(v, dict) and {"strategy", "db"} <= v.keys()]


def strategy_params(cfg: dict, pc: dict) -> dict:
    """Parameter ditulis langsung di bagian bot (`params`), atau diambil dari bagian strategi Fase 1 (s1/s2)."""
    return pc["params"] if "params" in pc else cfg[pc["strategy"]]


def paper_settings(cfg: dict, section: str = "paper") -> tuple[dict, BacktestConfig]:
    pc = cfg[section]
    bt = replace(BacktestConfig.from_config(cfg), initial_equity=float(pc["initial_equity"]))
    if "risk_per_trade" in pc:
        bt = replace(bt, risk_per_trade=float(pc["risk_per_trade"]))
    return pc, bt


def settings_hash(cfg: dict, section: str = "paper") -> str:
    """Semua yang memengaruhi hasil paper. Kalau berubah di tengah jalan, hasil jadi campuran."""
    pc = cfg[section]
    keep = {"paper": {k: v for k, v in pc.items() if k != "db"}, "strategy": strategy_params(cfg, pc),
            "costs": cfg["costs"], "risk": cfg["risk"]}
    return hashlib.sha256(yaml.safe_dump(keep, sort_keys=True).encode()).hexdigest()[:16]


def build_sleeves(cfg: dict, bars: dict, section: str = "paper") -> list[Sleeve]:
    pc = cfg[section]
    if "params" not in pc:  # strategi Fase 1 (nama sleeve "s2:BTC", dst.)
        return [make_sleeve(pc["strategy"], sym, bars[sym], cfg[pc["strategy"]]) for sym in pc["symbols"]]
    fn, params = REGISTRY[pc["strategy"]], pc["params"]
    hold = params.get("max_hold_days")
    return [Sleeve(f"{pc['strategy']}:{asset(sym)}", sym, fn(bars[sym], params), int(hold) if hold else None)
            for sym in pc["symbols"]]


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def get_meta(conn: sqlite3.Connection) -> dict[str, str]:
    return dict(conn.execute("SELECT key, value FROM meta").fetchall())


def set_meta(conn: sqlite3.Connection, **kv) -> None:
    conn.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", [(k, str(v)) for k, v in kv.items()])


def _append(conn: sqlite3.Connection, table: str, rows: list[dict]) -> None:
    if not rows:
        return
    df = pd.DataFrame(rows)
    for c in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[c]) or isinstance(df[c].iloc[0], pd.Timestamp):
            df[c] = df[c].map(lambda x: x.isoformat())
    df.to_sql(table, conn, if_exists="append", index=False)


# ---------------------------------------------------------------- inti

def run_paper(cfg: dict, bars: dict, funding: dict, db_path: Path, allow_config_change: bool = False,
              now: datetime | None = None, section: str = "paper") -> dict:
    """Proses semua candle close yang belum diproses. Satu transaksi SQLite: gagal = tidak ada yang berubah."""
    pc, bt = paper_settings(cfg, section)
    ks = pc["kill_switch"]
    sleeves = build_sleeves(cfg, bars, section)
    h = settings_hash(cfg, section)
    run_at = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")

    conn = open_db(db_path)
    try:
        with conn:
            meta = get_meta(conn)
            if "state" in meta:
                if meta["settings_hash"] != h and not allow_config_change:
                    raise SystemExit(
                        "[paper] config strategi/biaya/risiko berubah sejak paper trading dimulai "
                        f"(hash {meta['settings_hash']} -> {h}). Hasil akan tercampur dari dua setting. "
                        "Kembalikan config, atau mulai paper baru dengan file db lain.")
                eng = Engine.from_state(sleeves, bt, json.loads(meta["state"]))
                last_bar = pd.Timestamp(meta["last_bar"]) if meta.get("last_bar") else None
                peak = float(meta["peak_equity"])
            else:
                eng = Engine(sleeves, bt)
                last_bar = None
                peak = bt.initial_equity
                set_meta(conn, started_at=run_at, start=pc["start"], mode=pc["mode"])

            index = bars[pc["symbols"][0]].index
            for sym in pc["symbols"][1:]:
                index = index.intersection(bars[sym].index)
            index = index[index >= _utc(pc["start"])]
            if last_bar is not None:
                index = index[index > last_bar]
                expected = last_bar + bar_delta(cfg, section)
                if len(index) and index[0] != expected:
                    raise SystemExit(f"[paper] candle hilang: terakhir diproses {last_bar.date()}, "
                                     f"berikutnya tersedia {index[0].date()}. Jalankan update data dulu.")

            sig = {s.name: signal_arrays(s.signals, index) for s in sleeves}
            freq = PANDAS_FREQ[bot_timeframe(cfg, section)]
            fund = {sym: funding_by_day(funding[sym], freq) for sym in pc["symbols"]}
            notes: list[str] = []
            eq_rows = []
            for i, t in enumerate(index):
                eng.process_bar(
                    t,
                    prices={sym: tuple(bars[sym].loc[t, ["open", "high", "low", "close"]].astype(float))
                            for sym in pc["symbols"]},
                    funding={sym: fund[sym].get(t.value, ()) for sym in pc["symbols"]},
                    signals={n: tuple(a[i] for a in g) for n, g in sig.items()},
                )
                _, equity, cash, notional, n_open = eng.last_row
                peak = max(peak, equity)
                dd = 1 - equity / peak
                eq_rows.append((t.isoformat(), equity, cash, notional, n_open, dd))
                if dd >= ks["halt_drawdown"] and not eng.entries_blocked:
                    eng.entries_blocked = True
                    notes.append(f"KILL SWITCH aktif {t.date()}: drawdown {dd:.1%} >= {ks['halt_drawdown']:.0%}")
                elif dd >= ks["warn_drawdown"]:
                    notes.append(f"peringatan {t.date()}: drawdown {dd:.1%} >= {ks['warn_drawdown']:.0%}")

            conn.executemany("INSERT INTO equity VALUES (?, ?, ?, ?, ?, ?)", eq_rows)
            _append(conn, "trades", eng.trades)
            _append(conn, "fills", eng.fills)
            _append(conn, "rejections", eng.rejections)
            if len(index):
                last_bar = index[-1]
            closes = ({sym: float(bars[sym].loc[last_bar, "close"]) for sym in pc["symbols"]}
                      if last_bar is not None else {})
            positions = eng.open_positions(closes) if closes else []
            planned = eng.planned_orders()
            set_meta(conn, state=json.dumps(eng.to_state()), settings_hash=h, peak_equity=peak,
                     last_run=run_at, positions=json.dumps(positions), planned=json.dumps(planned),
                     **({"last_bar": last_bar.isoformat()} if last_bar is not None else {}))
            conn.execute("INSERT INTO runs VALUES (?, ?, ?, ?)",
                         (run_at, len(index), last_bar.isoformat() if last_bar is not None else None,
                          "; ".join(notes)))
    finally:
        conn.close()

    return {"processed": len(index), "last_bar": last_bar, "equity": eng.equity_prev, "peak": peak,
            "drawdown": 1 - eng.equity_prev / peak, "positions": positions, "planned": planned,
            "entries_blocked": eng.entries_blocked, "notes": notes,
            "new_trades": len(eng.trades), "new_fills": len(eng.fills),
            "fills": list(eng.fills), "trades": list(eng.trades)}


def reset_kill_switch(db_path: Path) -> None:
    conn = open_db(db_path)
    with conn:
        meta = get_meta(conn)
        if "state" not in meta:
            raise SystemExit("[paper] belum ada state paper trading.")
        state = json.loads(meta["state"])
        state["entries_blocked"] = False
        set_meta(conn, state=json.dumps(state))
        conn.execute("INSERT INTO runs VALUES (?, 0, ?, 'kill switch di-reset manual')",
                     (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), meta.get("last_bar")))
    conn.close()


# ---------------------------------------------------------------- CLI

def print_status(cfg: dict, r: dict, section: str = "paper") -> None:
    pc, bt = paper_settings(cfg, section)
    lb = r["last_bar"]
    print(f"\n=== Paper trading [{section}] {pc['strategy'].upper()}, {len(pc['symbols'])} simbol ({pc['mode']}) ===")
    if lb is None:
        start = _utc(pc["start"])
        print(f"Belum ada candle yang diproses. Candle pertama ({start.date()}) close pada "
              f"{(start + bar_delta(cfg, section)):%Y-%m-%d %H:%M} UTC; jalankan lagi setelah itu.")
        return
    print(f"Candle diproses run ini : {r['processed']} (terakhir {lb.date()})")
    print(f"Ekuitas                 : {r['equity']:,.2f} (awal {bt.initial_equity:,.0f}, "
          f"return {r['equity'] / bt.initial_equity - 1:+.2%}, drawdown {r['drawdown']:.1%})")
    print(f"Kill switch             : {'AKTIF, entry baru diblokir' if r['entries_blocked'] else 'tidak aktif'}")
    print(f"Trade/fill baru         : {r['new_trades']} trade selesai, {r['new_fills']} fill")
    print("\nPosisi terbuka:" if r["positions"] else "\nPosisi terbuka: tidak ada")
    for p in r["positions"]:
        print(f"  {p['symbol']} {p['side']} sejak {p['entry_time'][:10]} @ {p['entry_price']:,.2f}, "
              f"stop {p['stop']:,.2f}, close {p['last_close']:,.2f}, unrealized {p['unrealized']:+,.2f}")
    nxt = lb + bar_delta(cfg, section)
    print(f"\nRencana order di open {nxt:%Y-%m-%d %H:%M} UTC:" if r["planned"] else
          f"\nRencana order di open {nxt:%Y-%m-%d %H:%M} UTC: tidak ada")
    for o in r["planned"]:
        print(f"  {o['symbol']} {o['action']} {o['side']} ({o['reason']})")
    for n in r["notes"]:
        print(f"  ! {n}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bot", default="paper", help="bagian config bot (default: paper)")
    ap.add_argument("--all", action="store_true", help="jalankan semua bot paper di config, berurutan")
    ap.add_argument("--no-fetch", action="store_true", help="jangan update data dari exchange")
    ap.add_argument("--reset-kill-switch", action="store_true")
    ap.add_argument("--db", help="file db lain (mis. untuk demo); default dari config")
    ap.add_argument("--start", help="override tanggal mulai; hanya untuk db demo, bukan hasil forward")
    args = ap.parse_args()

    cfg = load_config()
    bots = bot_sections(cfg) if args.all else [args.bot]
    for b in bots:
        if b not in cfg:
            raise SystemExit(f"[paper] bot '{b}' tidak ada di config. Tersedia: {', '.join(bot_sections(cfg))}")
        if cfg[b]["mode"] != "dry_run":
            raise SystemExit(f"[paper] {b}: mode '{cfg[b]['mode']}' belum didukung; hanya dry_run.")
    if (args.db or args.start) and args.all:
        raise SystemExit("[paper] --db/--start hanya untuk satu bot, bukan --all.")
    if args.db:
        cfg[bots[0]]["db"] = args.db
    if args.start:
        if not args.db:
            raise SystemExit("[paper] --start hanya boleh bersama --db, supaya db forward test tidak tercampur.")
        cfg[bots[0]]["start"] = args.start

    if args.reset_kill_switch:
        for b in bots:
            reset_kill_switch(ROOT / cfg[b]["db"])
            print(f"[paper] {b}: kill switch di-reset; entry baru diizinkan lagi mulai run berikutnya.")
        return

    d = cfg["data"]
    report = not args.db  # db demo tidak memicu laporan Telegram
    need = {}  # timeframe -> simbol
    for b in bots:
        need.setdefault(bot_timeframe(cfg, b), set()).update(cfg[b]["symbols"])
    all_syms = sorted({s for v in need.values() for s in v})
    if not args.no_fetch:
        try:
            ex = connect(d, ROOT / d["dir"])
            for sym in all_syms:  # OHLCV harian + funding (dipakai semua timeframe)
                update_symbol(ex, d, sym, ROOT / d["dir"])
            for tf, syms in need.items():
                if tf != d["timeframe"]:
                    for sym in sorted(syms):
                        update_ohlcv(ex, d, sym, tf, ROOT / d["dir"])
        except (Exception, SystemExit) as e:  # noqa: BLE001
            if report:
                notify.alert(f"⚠️ Predix: gagal mengambil data harga ({e}). Robot tidak jalan; "
                             "akan dicoba lagi otomatis di jadwal berikutnya (tiap 4 jam).")
            raise
    bars = {tf: {s: load_bars(s, d, tf) for s in syms} for tf, syms in need.items()}
    funding = {s: load_funding(s, d) for s in all_syms}

    failed, processed, results = [], {}, {}
    for b in bots:  # satu bot gagal tidak menghentikan bot lain
        try:
            r = run_paper(cfg, bars[bot_timeframe(cfg, b)], funding, ROOT / cfg[b]["db"], section=b)
            processed[b] = r["processed"]
            results[b] = r
            print_status(cfg, r, b)
        except (Exception, SystemExit) as e:  # noqa: BLE001
            print(f"\n[paper] {b} GAGAL: {e}")
            failed.append(b)
    if report:
        notify.after_run(cfg, processed, failed, results=results)
    if failed:
        raise SystemExit(f"[paper] bot gagal: {', '.join(failed)}")


if __name__ == "__main__":
    main()

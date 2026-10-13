"""Ringkasan status robot dalam bahasa sehari-hari, dipakai Beranda dashboard dan pesan Telegram.

Hanya membaca database robot (read-only); tidak pernah mengubah apa pun.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pandas as pd

from src.data import ROOT, timeframe_ms

WIB = "Asia/Jakarta"
BULAN = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]
ALASAN = {"signal": "sinyal strategi", "reversal": "sinyal berbalik arah", "time": "batas waktu tahan",
          "stop": "kena stop loss", "end": "akhir periode"}
# Robot dianggap terlambat kalau candle kemarin belum diproses 30 menit setelah jadwal 00:05 UTC.
GRACE = pd.Timedelta(minutes=35)


# ---------------------------------------------------------------- timeframe robot

def bot_timeframe(cfg: dict, section: str) -> str:
    return cfg[section].get("timeframe", cfg["data"]["timeframe"])


def bar_delta(cfg: dict, section: str) -> pd.Timedelta:
    return pd.Timedelta(milliseconds=timeframe_ms(bot_timeframe(cfg, section)))


def is_daily(cfg: dict, section: str) -> bool:
    return bar_delta(cfg, section) >= pd.Timedelta(days=1)


# ---------------------------------------------------------------- format

def tanggal(ts, with_time: bool = False) -> str:
    t = pd.Timestamp(ts)
    t = (t.tz_localize("UTC") if t.tzinfo is None else t).tz_convert(WIB)
    s = f"{t.day} {BULAN[t.month - 1]} {t.year}"
    return s + f", {t:%H:%M} WIB" if with_time else s


def koin(symbol: str) -> str:
    return symbol.split("/")[0].split(":")[-1]


def angka(x: float, desimal: int = 2) -> str:
    """Format Indonesia: titik ribuan, koma desimal."""
    s = f"{x:,.{desimal}f}"
    return s.replace(",", "_").replace(".", ",").replace("_", ".")


def harga(x: float) -> str:
    return angka(x, 2 if x >= 1 else 4)


def persen(x: float) -> str:
    return ("+" if x > 0 else "") + angka(x * 100, 1) + "%"


def _utc_from_run(s: str | None) -> pd.Timestamp | None:
    return pd.Timestamp(s.replace(" UTC", ""), tz="UTC") if s else None


# ---------------------------------------------------------------- baca db

def read_db(path: Path) -> dict:
    """Baca db robot secara read-only (aman saat robot sedang menulis)."""
    if not Path(path).exists():
        return {"meta": {}, "equity": pd.DataFrame(), "trades": pd.DataFrame(), "fills": pd.DataFrame(),
                "rejections": pd.DataFrame(), "runs": pd.DataFrame()}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        get = lambda t: pd.read_sql(f"SELECT * FROM {t}", conn) if t in tables else pd.DataFrame()  # noqa: E731
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall()) if "meta" in tables else {}
        return {"meta": meta, **{t: get(t) for t in ("equity", "trades", "fills", "rejections", "runs")}}
    finally:
        conn.close()


def robot_sections(cfg: dict) -> list[str]:
    return [k for k, v in cfg.items()
            if k.startswith("paper") and isinstance(v, dict) and {"strategy", "db"} <= v.keys()]


def display_name(cfg: dict, section: str) -> tuple[str, str]:
    d = cfg.get("display", {}).get("robots", {}).get(section, {})
    return d.get("name", section), d.get("about", "")


# ---------------------------------------------------------------- kalimat

def kalimat_order(o: dict) -> str:
    c, side, act = koin(o["symbol"]), o["side"], o["action"]
    blocked = " (DIBLOKIR: kill switch aktif)" if "kill switch" in o.get("reason", "") else ""
    if act == "entry":
        return (f"Beli {c}" if side == "long" else f"Buka short {c} (untung kalau harga turun)") + blocked
    if act.startswith("entry"):  # reversal: buka posisi berlawanan satu hari kemudian
        return f"Besok: {'beli' if side == 'long' else 'buka short'} {c} ({ALASAN['reversal']})" + blocked
    alasan = ALASAN.get(o.get("reason", ""), o.get("reason", ""))
    return f"{'Jual' if side == 'long' else 'Tutup short'} {c} ({alasan})"


def kalimat_posisi(p: dict) -> str:
    d = 1 if p["side"] == "long" else -1
    chg = d * (p["last_close"] / p["entry_price"] - 1)
    jenis = "beli" if d > 0 else "short"
    return (f"{koin(p['symbol'])}: {jenis} sejak {tanggal(p['entry_time'])} di {harga(p['entry_price'])}, "
            f"sekarang {harga(p['last_close'])} ({persen(chg)}), stop loss {harga(p['stop'])}")


def kalimat_trade(t: pd.Series) -> str:
    jenis = "beli" if t["side"] == "long" else "short"
    hasil = "untung" if t["net_pnl"] > 0 else "rugi"
    return (f"{koin(t['symbol'])}: {jenis} {tanggal(t['entry_time'])} sampai {tanggal(t['exit_time'])}, "
            f"{hasil} {angka(abs(t['net_pnl']))} ({ALASAN.get(t['exit_reason'], t['exit_reason'])})")


# ---------------------------------------------------------------- ringkasan per robot

def _last_note(runs: pd.DataFrame) -> str:
    """Catatan dari run terakhir saja, supaya peringatan lama tidak terus diulang."""
    if runs.empty:
        return ""
    note = runs["note"].iloc[-1]
    return note if isinstance(note, str) else ""


def robot_summary(cfg: dict, section: str, now: pd.Timestamp | None = None) -> dict:
    pc = cfg[section]
    name, about = display_name(cfg, section)
    db = read_db(ROOT / pc["db"])
    meta, eq = db["meta"], db["equity"]
    now = now or pd.Timestamp.now(tz="UTC")
    start = pd.Timestamp(pc["start"], tz="UTC")
    initial = float(pc["initial_equity"])
    last_bar = pd.Timestamp(meta["last_bar"]) if meta.get("last_bar") else None
    last_run = _utc_from_run(meta.get("last_run"))
    # candle terakhir yang seharusnya sudah diproses saat ini
    delta = bar_delta(cfg, section)
    expected = (now - GRACE).floor(delta) - delta

    if not meta:
        status, status_text = "error", "Database robot belum ada di server ini."
    elif expected < start:
        first_run = start + delta + pd.Timedelta(minutes=5)
        first = "hari pertama" if delta >= pd.Timedelta(days=1) else "candle pertama"
        status, status_text = "wait", f"Menunggu {first}. Mulai bekerja {tanggal(first_run, True)}."
    elif last_bar is None or last_bar < expected:
        behind = int((expected - (last_bar if last_bar is not None else start - delta)) / delta)
        unit = "hari" if delta >= pd.Timedelta(days=1) else "candle"
        status, status_text = "late", f"Terlambat: {behind} {unit} belum diproses. Cek server."
    else:
        status, status_text = "ok", f"Jalan normal. Terakhir bekerja {tanggal(last_run, True)}."

    equity = float(eq["equity"].iloc[-1]) if len(eq) else initial
    state = json.loads(meta["state"]) if meta.get("state") else {}
    trades = db["trades"]
    recent = (trades.sort_values("exit_time").tail(3).iloc[::-1] if len(trades) else trades)
    return {
        "section": section, "name": name, "about": about,
        "coins": [koin(s) for s in pc["symbols"]],
        "status": status, "status_text": status_text,
        "start": tanggal(start), "initial": initial, "equity": equity, "change": equity / initial - 1,
        "drawdown": float(eq["drawdown"].iloc[-1]) if len(eq) else 0.0,
        "kill_switch": bool(state.get("entries_blocked", False)),
        "decision_day": (tanggal(last_bar + delta) if delta >= pd.Timedelta(days=1)
                         else tanggal(last_bar + delta, True)) if last_bar is not None else None,
        "timeframe": bot_timeframe(cfg, section),
        "orders": [kalimat_order(o) for o in json.loads(meta.get("planned", "[]"))],
        "positions": [kalimat_posisi(p) for p in json.loads(meta.get("positions", "[]"))],
        "recent_trades": [kalimat_trade(t) for _, t in recent.iterrows()],
        "n_trades": len(trades),
        "notes": _last_note(db["runs"]),
    }


# ---------------------------------------------------------------- untung/rugi harian

def daily_pnl(db: dict, initial: float) -> pd.DataFrame:
    """Untung/rugi per hari candle (UTC) = perubahan modal dari close kemarin ke close hari itu.
    Termasuk perubahan nilai posisi yang masih terbuka, fee, dan funding (bukan hanya trade yang ditutup)."""
    cols = ["date", "pnl", "pct", "equity", "trades"]
    eq = db["equity"]
    if eq.empty:
        return pd.DataFrame(columns=cols)
    eq = eq.assign(time=pd.to_datetime(eq["time"], utc=True)).sort_values("time")
    eq = eq.assign(date=eq["time"].dt.date).groupby("date", as_index=False).last()  # robot 4 jam: akhir hari
    prev = eq["equity"].shift(1).fillna(initial)
    out = pd.DataFrame({"date": eq["date"], "pnl": eq["equity"] - prev,
                        "pct": eq["equity"] / prev - 1, "equity": eq["equity"]})
    closed = {}
    tr = db["trades"]
    if len(tr):
        for _, t in tr.iterrows():
            d = pd.Timestamp(t["exit_time"]).date()
            hasil = "untung" if t["net_pnl"] > 0 else "rugi"
            closed.setdefault(d, []).append(f"{koin(t['symbol'])} ditutup, {hasil} {angka(abs(t['net_pnl']))}")
    out["trades"] = out["date"].map(lambda d: "; ".join(closed.get(d, [])))
    return out.reset_index(drop=True)


def combined_daily_pnl(cfg: dict) -> tuple[pd.DataFrame, float]:
    """Gabungan semua robot: untung/rugi harian dijumlahkan; persen terhadap total modal gabungan."""
    frames, initial = [], 0.0
    for sec in robot_sections(cfg):
        pc = cfg[sec]
        name, _ = display_name(cfg, sec)
        d = daily_pnl(read_db(ROOT / pc["db"]), float(pc["initial_equity"]))
        initial += float(pc["initial_equity"])
        if len(d):
            frames.append(d.assign(trades=d["trades"].map(lambda x, n=name: f"{n}: {x}" if x else "")))
    if not frames:
        return pd.DataFrame(columns=["date", "pnl", "pct", "equity", "trades"]), initial
    allr = pd.concat(frames)
    g = allr.groupby("date").agg(pnl=("pnl", "sum"), trades=("trades", lambda x: "; ".join(t for t in x if t)))
    g = g.sort_index()
    g["equity"] = initial + g["pnl"].cumsum()
    g["pct"] = g["pnl"] / (g["equity"] - g["pnl"])
    return g.reset_index()[["date", "pnl", "pct", "equity", "trades"]], initial


# ---------------------------------------------------------------- pesan Telegram

def kalimat_fill(f: dict) -> str:
    c = koin(f["symbol"])
    if f["action"] == "entry":
        aksi = f"Beli {c}" if f["side"] == "long" else f"Buka short {c}"
    else:
        aksi = (f"Jual {c}" if f["side"] == "long" else f"Tutup short {c}") + \
            f" ({ALASAN.get(f.get('reason', ''), f.get('reason', ''))})"
    return f"{aksi} di {harga(f['fill_price'])}, {tanggal(f['time'], True)}"


def trade_alert_text(cfg: dict, results: dict, sections: list[str]) -> str | None:
    """Pesan singkat hanya kalau robot intraday benar-benar bertransaksi di run ini."""
    blocks = []
    for sec in sections:
        r = results.get(sec) or {}
        if not r.get("fills"):
            continue
        name, _ = display_name(cfg, sec)
        lines = [f"🔔 {name}"] + [f"• {kalimat_fill(f)}" for f in r["fills"]]
        for t in r.get("trades", []):
            hasil = "untung" if t["net_pnl"] > 0 else "rugi"
            lines.append(f"  {koin(t['symbol'])} selesai: {hasil} {angka(abs(t['net_pnl']))}")
        blocks.append("\n".join(lines))
    return ("\n\n".join(blocks) + "\n\nSimulasi, uang virtual.") if blocks else None

def dashboard_url() -> str | None:
    """Alamat dashboard untuk laporan; disimpan di .env (DASHBOARD_URL), bukan di kode yang diunggah."""
    if os.environ.get("DASHBOARD_URL"):
        return os.environ["DASHBOARD_URL"]
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("DASHBOARD_URL="):
                return line.split("=", 1)[1].strip() or None
    return None


def telegram_text(cfg: dict, failed: list[str] | None = None, now: pd.Timestamp | None = None) -> str:
    app = cfg.get("display", {}).get("app_name", "Predix")
    now = now or pd.Timestamp.now(tz="UTC")
    lines = [f"{app} · laporan {tanggal(now)}", "Simulasi, uang virtual. Tidak ada order sungguhan.", ""]
    for sec in robot_sections(cfg):
        s = robot_summary(cfg, sec, now)
        icon = {"ok": "✅", "wait": "⏳", "late": "⚠️", "error": "❌"}[s["status"]]
        lines.append(f"{icon} {s['name']}")
        if sec in (failed or []):
            lines.append("❌ GAGAL jalan pagi ini. Cek log server.")
        if s["status"] != "ok":
            lines.append(s["status_text"])
        dp = daily_pnl(read_db(ROOT / cfg[sec]["db"]), s["initial"])
        if len(dp):
            last = dp.iloc[-1]
            lines.append(f"Hasil {tanggal(last['date'])}: {'+' if last['pnl'] >= 0 else '-'}{angka(abs(last['pnl']))} "
                         f"({persen(last['pct'])})")
        lines.append(f"Modal {angka(s['equity'])} ({persen(s['change'])} sejak {s['start']})")
        if s["kill_switch"]:
            lines.append("⛔ Kill switch aktif: tidak ada pembelian baru.")
        if s["decision_day"]:
            lines.append("Keputusan hari ini: " + ("; ".join(s["orders"]) if s["orders"] else "tidak ada, menunggu."))
        if s["positions"]:
            lines.append("Dipegang:")
            lines += [f"• {p}" for p in s["positions"]]
        if s["notes"]:
            lines.append(f"Catatan: {s['notes']}")
        lines.append("")
    url = dashboard_url()
    if url:
        lines.append(f"Detail: {url}")
    return "\n".join(lines)

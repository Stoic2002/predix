"""Riset: gaya trading trader top yang KONSISTEN di Hyperliquid (data on-chain publik).

    python -m src.hl_study

Aturan (ditetapkan 2026-10-14 sebelum melihat trader mana pun):
  1. Kandidat: 200 akun dengan PnL perp sepanjang masa terbesar di leaderboard publik, modal >= $100k.
  2. Konsisten: dari riwayat PnL perp (portfolio "perpAllTime"), untung di >= 60% bulan, riwayat >= 6 bulan.
     Diambil sampai 40 akun paling konsisten.
  3. Analisis sampai 10.000 fill terakhir per akun (batas API): lama memegang posisi, frekuensi, winrate,
     profit factor, long/short, koin, pemakaian taker (market) vs maker (limit), leverage saat ini.
  4. Privasi: alamat hanya disimpan di data/hyperliquid/ (tidak diunggah). Laporan memakai nama samaran.
Keterbatasan: hanya pemenang yang terlihat (survivorship), data fill terbatas 10.000 terakhir, kita melihat
APA yang dilakukan, bukan KENAPA. Bukan saran investasi dan bukan ajakan copy trading.
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

from src.data import ROOT

DATA = ROOT / "data" / "hyperliquid"
OUT = ROOT / "reports" / "hyperliquid"
API = "https://api.hyperliquid.xyz/info"
N_CANDIDATES, MIN_ACCOUNT, MIN_MONTHS, MIN_POS_FRAC, N_SELECT = 200, 100_000, 6, 0.60, 40
MAX_FILLS = 10_000


def info(payload: dict, retries: int = 4):
    for i in range(retries):
        try:
            req = urllib.request.Request(API, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
            return json.loads(urllib.request.urlopen(req, timeout=60).read())
        except Exception:  # noqa: BLE001  rate limit / jaringan: tunggu lalu coba lagi
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"gagal memanggil API: {payload['type']}")


def cached(name: str, fetch):
    p = DATA / name
    if p.exists():
        return json.loads(p.read_text())
    v = fetch()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(v))
    time.sleep(0.35)
    return v


# ---------------------------------------------------------------- seleksi

def candidates() -> list[dict]:
    rows = json.loads((DATA / "leaderboard.json").read_text())["leaderboardRows"]
    out = []
    for r in rows:
        w = dict(r["windowPerformances"])
        if float(r["accountValue"]) >= MIN_ACCOUNT and float(w["allTime"]["pnl"]) > 0:
            out.append({"address": r["ethAddress"], "account_value": float(r["accountValue"]),
                        "pnl_all": float(w["allTime"]["pnl"]), "vlm_month": float(w["month"]["vlm"])})
    return sorted(out, key=lambda x: x["pnl_all"], reverse=True)[:N_CANDIDATES]


def monthly_pnl(portfolio: list) -> pd.Series:
    hist = dict(portfolio).get("perpAllTime", {}).get("pnlHistory", [])
    if len(hist) < 3:
        return pd.Series(dtype=float)
    cum = pd.Series([float(v) for _, v in hist], index=pd.to_datetime([t for t, _ in hist], unit="ms"))
    m = cum.resample("ME").last().ffill()
    return m.diff().dropna()


# ---------------------------------------------------------------- fills -> posisi (episode)

def fetch_fills(addr: str) -> list[dict]:
    fills, start = [], 0
    while len(fills) < MAX_FILLS:
        batch = info({"type": "userFillsByTime", "user": addr, "startTime": start, "aggregateByTime": True})
        if not batch:
            break
        fills.extend(batch)
        nxt = max(f["time"] for f in batch) + 1
        if len(batch) < 2000 or nxt <= start:
            break
        start = nxt
        time.sleep(0.35)
    return fills


def episodes(fills: list[dict]) -> pd.DataFrame:
    """Satu episode = posisi satu koin dari 0 sampai kembali 0 (atau berbalik arah).
    Episode yang sudah terbuka sebelum jendela data dibuang (tidak lengkap)."""
    rows = []
    f = sorted(fills, key=lambda x: (x["coin"], x["time"]))
    open_ep: dict[str, dict] = {}
    for x in f:
        coin, sz = x["coin"], float(x["sz"])
        start_pos = float(x["startPosition"])
        signed = sz if x["side"] == "B" else -sz
        end_pos = start_pos + signed
        pnl = float(x["closedPnl"]) - float(x["fee"])
        notional = sz * float(x["px"])
        ep = open_ep.get(coin)
        if ep is None:
            if abs(start_pos) > 1e-12:  # posisi sudah ada sebelum data: tidak lengkap
                if abs(end_pos) < 1e-12 or np.sign(end_pos) != np.sign(start_pos):
                    pass
                continue
            ep = {"coin": coin, "side": "long" if signed > 0 else "short", "start": x["time"], "pnl": 0.0,
                  "max_notional": 0.0, "taker": 0, "fills": 0}
            open_ep[coin] = ep
        ep["pnl"] += pnl
        ep["fills"] += 1
        ep["taker"] += int(bool(x["crossed"]))
        ep["max_notional"] = max(ep["max_notional"], abs(end_pos) * float(x["px"]), notional)
        flipped = abs(start_pos) > 1e-12 and np.sign(end_pos) != np.sign(start_pos) and abs(end_pos) > 1e-12
        if abs(end_pos) < 1e-12 or flipped:
            rows.append({**ep, "end": x["time"]})
            del open_ep[coin]
            if flipped:  # sisa posisi berbalik arah = episode baru
                open_ep[coin] = {"coin": coin, "side": "long" if end_pos > 0 else "short", "start": x["time"],
                                 "pnl": 0.0, "max_notional": abs(end_pos) * float(x["px"]), "taker": 0, "fills": 0}
    df = pd.DataFrame(rows)
    if len(df):
        df["hold_hours"] = (df["end"] - df["start"]) / 3_600_000
    return df


def style(hold_median_h: float, taker_share: float, fills_per_day: float, n_episodes: int) -> str:
    """Gaya dari frekuensi dulu (data 10.000 fill terakhir), baru dari lama memegang posisi."""
    if fills_per_day > 1000:
        return ("HFT/market maker (limit order)" if taker_share < 0.5 else "HFT taker (market order)")
    if fills_per_day > 100 and taker_share < 0.2:
        return "market maker (limit order)"
    if n_episodes < 5:
        return "posisi besar dikelola terus (jarang tutup penuh)"
    if hold_median_h < 1:
        return "scalper (< 1 jam)"
    if hold_median_h < 24:
        return "intraday (1–24 jam)"
    if hold_median_h < 24 * 14:
        return "swing (1–14 hari)"
    return "posisi (> 14 hari)"


# ---------------------------------------------------------------- main

def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    cands = candidates()
    print(f"[hl] kandidat: {len(cands)} akun (PnL sepanjang masa terbesar, modal >= ${MIN_ACCOUNT:,})")
    scored = []
    for i, c in enumerate(cands, 1):
        pf = cached(f"portfolio/{c['address']}.json", lambda a=c["address"]: info({"type": "portfolio", "user": a}))
        m = monthly_pnl(pf)
        c.update(months=len(m), pos_frac=float((m > 0).mean()) if len(m) else 0.0,
                 worst_month=float(m.min()) if len(m) else 0.0)
        scored.append(c)
        if i % 50 == 0:
            print(f"[hl] riwayat profit: {i}/{len(cands)}")
    cons = [c for c in scored if c["months"] >= MIN_MONTHS and c["pos_frac"] >= MIN_POS_FRAC]
    cons = sorted(cons, key=lambda c: (c["pos_frac"], c["pnl_all"]), reverse=True)[:N_SELECT]
    print(f"[hl] konsisten (>= {MIN_POS_FRAC:.0%} bulan untung, >= {MIN_MONTHS} bulan): "
          f"{sum(1 for c in scored if c['months'] >= MIN_MONTHS and c['pos_frac'] >= MIN_POS_FRAC)} akun; dianalisis {len(cons)}")

    rows, all_eps = [], []
    for i, c in enumerate(cons, 1):
        alias = f"Trader {i:02d}"
        fills = cached(f"fills/{c['address']}.json", lambda a=c["address"]: fetch_fills(a))
        state = cached(f"state/{c['address']}.json", lambda a=c["address"]: info({"type": "clearinghouseState", "user": a}))
        if not fills:
            continue
        t = pd.to_datetime([x["time"] for x in fills], unit="ms")
        days = max((t.max() - t.min()).total_seconds() / 86400, 1 / 24)
        eps = episodes(fills)
        notional = sum(float(x["sz"]) * float(x["px"]) for x in fills)
        coin_ntl = pd.Series({k: sum(float(x["sz"]) * float(x["px"]) for x in fills if x["coin"] == k)
                              for k in {x["coin"] for x in fills}}).sort_values(ascending=False)
        taker = float(np.mean([bool(x["crossed"]) for x in fills]))
        acct = float(state.get("marginSummary", {}).get("accountValue", 0) or 0)
        ntl_now = float(state.get("marginSummary", {}).get("totalNtlPos", 0) or 0)
        levs = [float(p["position"]["leverage"]["value"]) for p in state.get("assetPositions", [])
                if p.get("position", {}).get("leverage")]
        if len(eps):
            wins, losses = eps.pnl[eps.pnl > 0], eps.pnl[eps.pnl < 0]
            hold = float(eps.hold_hours.median())
            row_eps = {"episodes": len(eps), "hold_median_h": hold, "hold_p90_h": float(eps.hold_hours.quantile(0.9)),
                       "winrate": float((eps.pnl > 0).mean()),
                       "profit_factor": float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() < 0 else np.nan,
                       "win_loss_ratio": float(wins.mean() / -losses.mean()) if len(wins) and len(losses) else np.nan,
                       "long_share": float((eps.side == "long").mean()),
                       "pnl_long": float(eps.loc[eps.side == "long", "pnl"].sum()),
                       "pnl_short": float(eps.loc[eps.side == "short", "pnl"].sum())}
            all_eps.append(eps.assign(trader=alias))
        else:
            hold = np.nan
            row_eps = {"episodes": 0}
        rows.append({
            "trader": alias, "account_value": c["account_value"], "pnl_all_time": c["pnl_all"],
            "months": c["months"], "profitable_months": c["pos_frac"], "worst_month": c["worst_month"],
            "fills": len(fills), "window_days": days, "fills_per_day": len(fills) / days,
            "taker_share": taker, "coins": int(coin_ntl.size), "top_coin": coin_ntl.index[0],
            "top3_share": float(coin_ntl.head(3).sum() / coin_ntl.sum()),
            "btc_eth_share": float(coin_ntl.reindex(["BTC", "ETH"]).fillna(0).sum() / coin_ntl.sum()),
            "turnover_per_day": notional / days / max(c["account_value"], 1),
            "exposure_now": ntl_now / acct if acct else np.nan,
            "max_leverage_setting": max(levs) if levs else np.nan,
            **row_eps,
            "style": style(hold if hold == hold else 0, taker, len(fills) / days, row_eps.get("episodes", 0)),
        })
        print(f"[hl] {alias}: {len(fills)} fill, {row_eps.get('episodes', 0)} posisi selesai")
    df = pd.DataFrame(rows)
    df.to_csv(OUT / "traders.csv", index=False)
    if all_eps:
        e = pd.concat(all_eps)
        e["entry_hour_utc"] = pd.to_datetime(e["start"], unit="ms").dt.hour
        e.drop(columns=["start", "end"]).to_csv(OUT / "episodes.csv", index=False)
    meta = {"leaderboard_accounts": len(json.loads((DATA / "leaderboard.json").read_text())["leaderboardRows"]),
            "candidates": len(cands), "consistent": len(cons), "rules": __doc__.split("Aturan")[1].split("Keterbatasan")[0]}
    (OUT / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
    print(f"[hl] selesai: {OUT.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()

"""M1 — Data: ambil OHLCV harian + riwayat funding, update inkremental, validasi.

Jalankan dari root proyek:
    python -m src.data                 # fetch/update lalu validasi
    python -m src.data --validate-only # validasi CSV yang sudah ada, tanpa jaringan

Hanya memakai endpoint publik. Tidak ada API key, login, atau order.
Anomali dilaporkan, tidak diperbaiki diam-diam.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import ccxt
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
OHLCV_COLS = ["timestamp", "open", "high", "low", "close", "volume"]
FUNDING_COLS = ["timestamp", "funding_rate"]
HOUR_MS = 3_600_000


# ---------------------------------------------------------------- config & path

def load_config(path: Path = ROOT / "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def symbol_slug(symbol: str) -> str:
    """'BTC/USDT:USDT' -> 'BTC_USDT_USDT' (aman untuk nama file)."""
    return symbol.replace("/", "_").replace(":", "_")


def ohlcv_path(data_dir: Path, symbol: str, timeframe: str) -> Path:
    return data_dir / f"{symbol_slug(symbol)}_{timeframe}.csv"


def funding_path(data_dir: Path, symbol: str) -> Path:
    return data_dir / f"{symbol_slug(symbol)}_funding.csv"


def timeframe_ms(timeframe: str) -> int:
    return ccxt.Exchange.parse_timeframe(timeframe) * 1000


def to_utc_str(ts_ms: pd.Series) -> pd.Series:
    return pd.to_datetime(ts_ms, unit="ms", utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_ts(ts_ms: int) -> str:
    return pd.Timestamp(int(ts_ms), unit="ms", tz="UTC").strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------- sumber data

def read_meta(data_dir: Path) -> dict:
    p = data_dir / "meta.json"
    return json.loads(p.read_text()) if p.exists() else {}


def write_meta(data_dir: Path, meta: dict) -> None:
    (data_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")


def connect(cfg: dict, data_dir: Path) -> ccxt.Exchange:
    """Sambung ke exchange publik. Kalau data lama sudah ada, wajib pakai sumber yang
    sama supaya data dari dua exchange tidak tercampur dalam satu file."""
    locked = read_meta(data_dir).get("exchange")
    names = [locked] if locked else [cfg["exchange"], cfg["fallback_exchange"]]
    for name in names:
        try:
            ex = getattr(ccxt, name)({"enableRateLimit": True})
            ex.load_markets()
            print(f"[data] terhubung ke {name} (publik, tanpa API key)")
            return ex
        except (ccxt.ExchangeNotAvailable, ccxt.ExchangeError) as e:
            print(f"[data] {name} gagal: {type(e).__name__}: {str(e)[:200]}")
    hint = (f" Data lama berasal dari '{locked}'; ganti sumber berarti hapus data/ dan unduh ulang."
            if locked else "")
    raise SystemExit("[data] tidak ada exchange yang bisa dipakai." + hint)


# ---------------------------------------------------------------- fetch

def fetch_ohlcv_since(ex: ccxt.Exchange, symbol: str, timeframe: str, since: int) -> pd.DataFrame:
    """Pagination maju sampai data habis. Candle yang belum close dibuang."""
    tf_ms = timeframe_ms(timeframe)
    rows: list[list] = []
    while since <= ex.milliseconds():
        batch = ex.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
        if not batch:
            break
        rows.extend(batch)
        nxt = batch[-1][0] + tf_ms
        if nxt <= since:  # tidak ada kemajuan, cegah loop tanpa akhir
            break
        since = nxt
    df = pd.DataFrame(rows, columns=OHLCV_COLS)
    closed = df["timestamp"] + tf_ms <= ex.milliseconds()
    return df[closed]


def fetch_funding_since(ex: ccxt.Exchange, symbol: str, since: int) -> pd.DataFrame:
    rows: list[tuple] = []
    while since <= ex.milliseconds():
        batch = ex.fetch_funding_rate_history(symbol, since=since, limit=1000)
        if not batch:
            break
        rows.extend((r["timestamp"], r["fundingRate"]) for r in batch)
        nxt = batch[-1]["timestamp"] + 1
        if nxt <= since:
            break
        since = nxt
    return pd.DataFrame(rows, columns=FUNDING_COLS)


# ---------------------------------------------------------------- simpan (inkremental)

def read_csv(path: Path, cols: list[str]) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=cols)
    return pd.read_csv(path)[cols]


def merge_save(path: Path, old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Gabung data lama + baru, buang duplikat timestamp, urutkan, simpan."""
    frames = [f for f in (old, new) if not f.empty]
    df = pd.concat(frames) if frames else old
    df = df.drop_duplicates("timestamp", keep="last").sort_values("timestamp").reset_index(drop=True)
    df["timestamp"] = df["timestamp"].astype("int64")
    out = df.copy()
    out.insert(1, "datetime", to_utc_str(out["timestamp"]))
    out.to_csv(path, index=False)
    return df


def update_symbol(ex: ccxt.Exchange, cfg: dict, symbol: str, data_dir: Path) -> None:
    tf = cfg["timeframe"]
    start_ms = ex.parse8601(f"{cfg['start']}T00:00:00Z")

    p = ohlcv_path(data_dir, symbol, tf)
    old = read_csv(p, OHLCV_COLS)
    since = int(old["timestamp"].max()) + timeframe_ms(tf) if not old.empty else start_ms
    new = fetch_ohlcv_since(ex, symbol, tf, since)
    df = merge_save(p, old, new)
    print(f"[data] {symbol} OHLCV: +{len(new)} candle baru, total {len(df)} -> {p.relative_to(ROOT)}")

    p = funding_path(data_dir, symbol)
    old = read_csv(p, FUNDING_COLS)
    since = int(old["timestamp"].max()) + 1 if not old.empty else start_ms
    new = fetch_funding_since(ex, symbol, since)
    df = merge_save(p, old, new)
    print(f"[data] {symbol} funding: +{len(new)} event baru, total {len(df)} -> {p.relative_to(ROOT)}")


# ---------------------------------------------------------------- loader untuk backtest

def _time_indexed(df: pd.DataFrame) -> pd.DataFrame:
    df.index = pd.DatetimeIndex(pd.to_datetime(df["timestamp"], unit="ms", utc=True), name="time")
    return df.drop(columns="timestamp")


def load_bars(symbol: str, cfg: dict | None = None, timeframe: str | None = None) -> pd.DataFrame:
    """OHLCV dari CSV, berindeks waktu open candle (UTC). Default timeframe dari config (1d)."""
    cfg = cfg or load_config()["data"]
    return _time_indexed(read_csv(ohlcv_path(ROOT / cfg["dir"], symbol, timeframe or cfg["timeframe"]), OHLCV_COLS))


def update_ohlcv(ex, cfg: dict, symbol: str, timeframe: str, data_dir: Path) -> int:
    """Tambah candle baru (yang sudah close) untuk timeframe tertentu; kembalikan jumlah candle baru."""
    p = ohlcv_path(data_dir, symbol, timeframe)
    old = read_csv(p, OHLCV_COLS)
    start_ms = ex.parse8601(f"{cfg['start']}T00:00:00Z")
    since = int(old["timestamp"].max()) + timeframe_ms(timeframe) if not old.empty else start_ms
    new = fetch_ohlcv_since(ex, symbol, timeframe, since)
    merge_save(p, old, new)
    return len(new)


def load_funding(symbol: str, cfg: dict | None = None) -> pd.DataFrame:
    """Riwayat funding dari CSV, berindeks waktu event (UTC)."""
    cfg = cfg or load_config()["data"]
    return _time_indexed(read_csv(funding_path(ROOT / cfg["dir"], symbol), FUNDING_COLS))


# ---------------------------------------------------------------- validasi

def _sample(ts: pd.Series, n: int = 5) -> str:
    vals = [fmt_ts(t)[:10] for t in ts.head(n)]
    more = f" (+{len(ts) - n} lagi)" if len(ts) > n else ""
    return ", ".join(vals) + more


def validate_ohlcv(df: pd.DataFrame, timeframe: str, start: str) -> list[str]:
    issues: list[str] = []
    if df.empty:
        return ["file kosong"]
    tf_ms = timeframe_ms(timeframe)
    ts = df["timestamp"]

    if (n := ts.duplicated().sum()):
        issues.append(f"{n} timestamp duplikat")
    if (bad := ts[ts % tf_ms != 0]).size:
        issues.append(f"{bad.size} candle tidak tepat di batas {timeframe} UTC: {_sample(bad)}")

    expected = pd.RangeIndex(int(ts.min()), int(ts.max()) + tf_ms, tf_ms)
    missing = pd.Series(sorted(set(expected) - set(ts)), dtype="int64")
    if missing.size:
        issues.append(f"{missing.size} candle hilang: {_sample(missing)}")

    if (bad := ts[df[OHLCV_COLS].isna().any(axis=1)]).size:
        issues.append(f"{bad.size} baris berisi NaN: {_sample(bad)}")
    if (bad := ts[(df[["open", "high", "low", "close"]] <= 0).any(axis=1)]).size:
        issues.append(f"{bad.size} candle dengan harga nol/negatif: {_sample(bad)}")
    if (bad := ts[df["volume"] <= 0]).size:
        issues.append(f"{bad.size} candle dengan volume nol/negatif: {_sample(bad)}")
    if (bad := ts[df["high"] < df[["open", "close"]].max(axis=1)]).size:
        issues.append(f"{bad.size} candle high < max(open, close): {_sample(bad)}")
    if (bad := ts[df["low"] > df[["open", "close"]].min(axis=1)]).size:
        issues.append(f"{bad.size} candle low > min(open, close): {_sample(bad)}")

    start_ms = int(pd.Timestamp(start, tz="UTC").value // 1_000_000)
    if ts.min() > start_ms:
        issues.append(f"data baru mulai {fmt_ts(ts.min())[:10]}, bukan {start} (info)")
    return issues


def describe_funding(df: pd.DataFrame) -> tuple[list[str], list[str]]:
    """Kembalikan (info, issues). Interval dibaca dari data, tidak diasumsikan 8 jam."""
    if df.empty:
        return [], ["file kosong"]
    issues: list[str] = []
    ts = df["timestamp"]
    if (n := ts.duplicated().sum()):
        issues.append(f"{n} timestamp duplikat")
    if (bad := ts[df["funding_rate"].isna()]).size:
        issues.append(f"{bad.size} funding rate NaN: {_sample(bad)}")

    gaps_h = (ts.diff().dropna() / HOUR_MS).round(2)
    dist = gaps_h.round().astype(int).value_counts().sort_index()
    info = [
        "interval (jam -> jumlah): " + ", ".join(f"{h}h -> {c}" for h, c in dist.items()),
        f"rate min {df['funding_rate'].min():.6f}, max {df['funding_rate'].max():.6f}, "
        f"rata-rata {df['funding_rate'].mean():.6f}",
    ]
    off_hour = int((ts % HOUR_MS != 0).sum())
    if off_hour:
        info.append(f"{off_hour} timestamp tidak tepat di jam bulat (selisih milidetik dari exchange)")

    # Interval yang jarang muncul (< 1% event) kemungkinan celah data atau perubahan jadwal.
    rare = dist[dist < max(1, 0.01 * len(gaps_h))].index
    odd = gaps_h[gaps_h.round().astype(int).isin(rare)]
    if odd.size:
        where = ", ".join(f"{fmt_ts(ts.loc[i])} ({g:g}h)" for i, g in odd.head(5).items())
        issues.append(f"{odd.size} interval tidak biasa, di: {where}")
    return info, issues


def all_symbols(full_cfg: dict) -> list[str]:
    """Simbol Fase 1 + universe riset, tanpa duplikat, urutan tetap."""
    r = full_cfg.get("research", {})
    return list(dict.fromkeys(full_cfg["data"]["symbols"] + r.get("universe", [])
                              + r.get("fresh_holdout", {}).get("universe", [])))


def report(cfg: dict, data_dir: Path, symbols: list[str]) -> None:
    print("\n=== Ringkasan validasi ===")
    now_ms = int(time.time() * 1000)
    for symbol in symbols:
        print(f"\n{symbol}")
        p = ohlcv_path(data_dir, symbol, cfg["timeframe"])
        if not p.exists():
            print(f"  OHLCV: file tidak ada ({p.relative_to(ROOT)})")
        else:
            df = read_csv(p, OHLCV_COLS)
            print(f"  OHLCV  : {len(df)} candle, {fmt_ts(df.timestamp.min())} s/d {fmt_ts(df.timestamp.max())}")
            for msg in validate_ohlcv(df, cfg["timeframe"], cfg["start"]) or ["OK, tidak ada anomali"]:
                print(f"    - {msg}")

        p = funding_path(data_dir, symbol)
        if not p.exists():
            print(f"  Funding: file tidak ada ({p.relative_to(ROOT)})")
            continue
        df = read_csv(p, FUNDING_COLS)
        print(f"  Funding: {len(df)} event, {fmt_ts(df.timestamp.min())} s/d {fmt_ts(df.timestamp.max())}")
        info, issues = describe_funding(df)
        age_h = (now_ms - df.timestamp.max()) / HOUR_MS
        if age_h > 24:
            issues.append(f"event funding terakhir {age_h:.0f} jam lalu, kemungkinan pagination berhenti lebih awal")
        for msg in info:
            print(f"    · {msg}")
        for msg in issues or ["OK, tidak ada anomali"]:
            print(f"    - {msg}")


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--validate-only", action="store_true", help="lewati fetch, hanya validasi CSV")
    args = ap.parse_args()

    full = load_config()
    cfg = full["data"]
    symbols = all_symbols(full)
    data_dir = ROOT / cfg["dir"]
    data_dir.mkdir(exist_ok=True)

    if not args.validate_only:
        ex = connect(cfg, data_dir)
        for symbol in symbols:
            update_symbol(ex, cfg, symbol, data_dir)
        write_meta(data_dir, {"exchange": ex.id, "last_update_utc": fmt_ts(ex.milliseconds())})

    report(cfg, data_dir, symbols)


if __name__ == "__main__":
    main()

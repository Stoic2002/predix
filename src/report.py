"""M5 — Laporan: grafik + tabel + kesimpulan per kriteria, dari hasil M4 (tanpa menjalankan ulang OOS).

    python -m src.report

Membaca reports/m4/*.csv dan reports/notes.md, menulis reports/REPORT.md dan reports/charts/*.png.
"""
from __future__ import annotations

import json
import math
import re

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.ticker as mtick  # noqa: E402
import pandas as pd  # noqa: E402

from src import metrics  # noqa: E402
from src.data import ROOT, load_bars, load_config  # noqa: E402
from src.run import asset  # noqa: E402

M4 = ROOT / "reports" / "m4"
REPORTS = ROOT / "reports"
CHARTS = REPORTS / "charts"

# Palet referensi (light), slot kategorikal dipakai berurutan dan mengikuti entitas.
SURFACE, INK, INK_2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3de"
COLORS = {"s1": "#2a78d6", "s2": "#eb6834", "portfolio": "#1baf7a",
          "buyhold:BTC": "#eda100", "buyhold:ETH": "#e87ba4"}
NAMES = {"s1": "S1 mean reversion", "s2": "S2 trend", "portfolio": "Portofolio",
         "buyhold:BTC": "Buy & hold BTC", "buyhold:ETH": "Buy & hold ETH"}

PCT = {"total_return", "cagr", "max_drawdown", "time_in_market", "winrate"}
INT = {"longest_dd_days", "n_trades", "longest_losing_streak", "n_long", "n_short", "n_rejected"}
LABELS = {
    "total_return": "Total return", "cagr": "CAGR", "max_drawdown": "Max drawdown",
    "longest_dd_days": "Durasi drawdown terlama (hari)", "calmar": "Calmar",
    "sharpe": "Sharpe (harian, ×√365)", "profit_factor": "Profit factor",
    "expectancy_r": "Expectancy (R)", "winrate": "Winrate", "avg_win": "Rata-rata win ($)",
    "avg_loss": "Rata-rata loss ($)", "avg_win_r": "Rata-rata win (R)", "avg_loss_r": "Rata-rata loss (R)",
    "n_trades": "Jumlah trade", "avg_bars_held": "Rata-rata lama posisi (hari)",
    "time_in_market": "Persentase waktu terbuka", "total_fees": "Total fee ($)",
    "total_funding": "Total funding ($, + = dibayar)", "total_slippage": "Total slippage ($)",
    "longest_losing_streak": "Losing streak terpanjang", "n_long": "Trade long", "n_short": "Trade short",
    "pnl_long": "PnL long ($)", "pnl_short": "PnL short ($)",
    "first_dd_20pct": "Pertama kali drawdown ≥ 20%", "first_dd_30pct": "Pertama kali drawdown ≥ 30%",
    "n_rejected": "Hari sinyal ditolak (cek likuidasi)",
}


# ---------------------------------------------------------------- format

def fmt(metric: str, v) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return str(v)
    if math.isnan(x):
        return "-"
    if math.isinf(x):
        return "∞"
    if metric in PCT:
        return f"{x:.1%}"
    if metric in INT:
        return f"{x:,.0f}"
    return f"{x:,.2f}"


def md_table(header: list[str], rows: list[list[str]], align_right_from: int = 1) -> str:
    sep = ["---" if i < align_right_from else "---:" for i in range(len(header))]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(sep) + " |"]
    lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


def metrics_md(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    rows = [[LABELS.get(m, m), *[fmt(m, df.at[m, c]) for c in cols]] for m in df.index if m in LABELS]
    return md_table(["Metrik", *cols], rows)


# ---------------------------------------------------------------- data

def chained_equity(initial: float) -> pd.DataFrame:
    """Gabung run IS dan OOS jadi satu kurva: OOS diskalakan ke ekuitas akhir IS."""
    eq_is = pd.read_csv(M4 / "equity_is.csv", index_col=0, parse_dates=True)
    eq_oos = pd.read_csv(M4 / "equity_oos.csv", index_col=0, parse_dates=True)
    return pd.concat([eq_is, eq_oos * eq_is.iloc[-1] / initial])


# ---------------------------------------------------------------- grafik

def _style(ax) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=8, length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def _figure(nrows=1, ncols=1, **kw):
    fig, axes = plt.subplots(nrows, ncols, facecolor=SURFACE, **kw)
    for ax in (axes.flat if hasattr(axes, "flat") else [axes]):
        _style(ax)
    return fig, axes


def _title(fig, title: str, subtitle: str) -> float:
    """Judul + subjudul dengan jarak tetap dalam inci; kembalikan batas atas area plot."""
    h = fig.get_figheight()
    fig.text(0.01, 1 - 0.08 / h, title, ha="left", va="top", fontsize=11,
             fontweight="bold", color=INK)
    fig.text(0.01, 1 - 0.36 / h, subtitle, ha="left", va="top", fontsize=8.5, color=INK_2)
    return 1 - 0.62 / h


def _shade_oos(ax, oos_start, end) -> None:
    ax.axvspan(oos_start, end, color="#f0efea", zorder=0, linewidth=0)


def _end_labels(ax, series: dict[str, pd.Series], fmt_value) -> None:
    """Label langsung di ujung garis dalam warna teks; penanda ■ berwarna membawa identitas.
    Tabrakan dihindari di ruang sumbu (log-aware) supaya label tidak keluar dari plot."""
    log = ax.get_yscale() == "log"
    f = (lambda v: math.log10(v)) if log else (lambda v: v)
    inv = (lambda v: 10 ** v) if log else (lambda v: v)
    lo, hi = (f(v) for v in ax.get_ylim())
    min_gap = (hi - lo) * 0.06
    placed: list[float] = []
    for y, k in sorted(((f(s.iloc[-1]), k) for k, s in series.items()), reverse=True):
        for p in placed:
            if abs(y - p) < min_gap:
                y = p - min_gap
        placed.append(y)
        x = series[k].index[-1]
        ax.annotate("■", (x, inv(y)), xytext=(6, 0), textcoords="offset points", va="center",
                    fontsize=7.5, color=COLORS[k])
        ax.annotate(f"{NAMES[k]}  {fmt_value(series[k].iloc[-1])}", (x, inv(y)), xytext=(15, 0),
                    textcoords="offset points", va="center", fontsize=7.5, color=INK_2)


def _clip_xticks(ax, first, last) -> None:
    """Jangan tampilkan tick tahun di luar rentang data (padding dipakai untuk label)."""
    lo, hi = mdates.date2num(first), mdates.date2num(last)
    ax.set_xticks([t for t in ax.get_xticks() if lo <= t <= hi])
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))


def chart_equity(eq: pd.DataFrame, oos_start, initial: float) -> str:
    keys = ["s1", "s2", "portfolio"]
    fig, (a1, a2) = _figure(2, 1, figsize=(10, 6.2), sharex=True, gridspec_kw={"height_ratios": [3, 1.4]})
    for k in keys:
        a1.plot(eq.index, eq[k], color=COLORS[k], linewidth=1.3, label=NAMES[k])
        dd = metrics.drawdown(eq[k]) * 100
        a2.plot(eq.index, dd, color=COLORS[k], linewidth=1.1)
    a1.axhline(initial, color=INK_2, linewidth=0.6, linestyle=(0, (2, 2)))
    a1.yaxis.set_major_formatter(mtick.StrMethodFormatter("{x:,.0f}"))
    a1.set_ylabel("Ekuitas ($)", fontsize=8, color=INK_2)
    a2.yaxis.set_major_formatter(mtick.PercentFormatter(decimals=0))
    a2.set_ylabel("Drawdown", fontsize=8, color=INK_2)
    for ax in (a1, a2):
        _shade_oos(ax, oos_start, eq.index[-1])
    a1.text(oos_start, a1.get_ylim()[1], "  out-of-sample →", va="top", fontsize=8, color=INK_2)
    a1.legend(loc="upper left", frameon=False, fontsize=8, labelcolor=INK_2)
    a1.set_xlim(eq.index[0], eq.index[-1] + pd.Timedelta(days=330))
    _end_labels(a1, {k: eq[k] for k in keys}, lambda v: f"{v:,.0f}")
    _clip_xticks(a2, eq.index[0], eq.index[-1])
    top = _title(fig, "Kurva ekuitas dan drawdown, setelah semua biaya",
           f"Ekuitas awal {initial:,.0f}; IS 2020–2023, OOS diarsir. "
           "Run per strategi berisiko 0,5%/trade per simbol, portofolio 0,25%/trade per sleeve.")
    fig.tight_layout(rect=(0, 0, 1, top))
    return _save(fig, "equity_drawdown.png")


def chart_sleeves(eq: pd.DataFrame, oos_start, initial: float) -> str:
    sleeves = [c for c in eq.columns if ":" in c]
    fig, axes = _figure(2, 2, figsize=(10, 5.6), sharex=True, sharey=True)
    for ax, col in zip(axes.flat, sleeves):
        st = col.split(":")[0]
        ax.plot(eq.index, eq[col], color=COLORS[st], linewidth=1.2)
        ax.axhline(initial, color=INK_2, linewidth=0.6, linestyle=(0, (2, 2)))
        _shade_oos(ax, oos_start, eq.index[-1])
        ax.set_title(f"{NAMES[st]} · {col.split(':')[1]}   akhir {eq[col].iloc[-1]:,.0f}",
                     fontsize=8.5, color=INK, loc="left")
        ax.yaxis.set_major_formatter(mtick.StrMethodFormatter("{x:,.0f}"))
    for ax in axes[-1]:
        _clip_xticks(ax, eq.index[0], eq.index[-1])
    top = _title(fig, "Ekuitas per strategi per simbol",
           "Tiap panel run terpisah dengan risiko 1%/trade. Area diarsir = out-of-sample.")
    fig.tight_layout(rect=(0, 0, 1, top))
    return _save(fig, "sleeves.png")


def chart_benchmark(eq: pd.DataFrame, bh: dict[str, pd.Series], oos_start) -> str:
    fig, ax = _figure(figsize=(10, 4.6))
    series = {"portfolio": eq["portfolio"], "s2": eq["s2"], **bh}
    for k, s in series.items():
        ax.plot(s.index, s, color=COLORS[k], linewidth=1.3)
    ax.set_yscale("log")
    ax.yaxis.set_major_formatter(mtick.StrMethodFormatter("{x:,.0f}"))
    _shade_oos(ax, oos_start, eq.index[-1])
    ax.set_xlim(eq.index[0], eq.index[-1] + pd.Timedelta(days=420))
    _end_labels(ax, series, lambda v: f"{v:,.0f}")
    _clip_xticks(ax, eq.index[0], eq.index[-1])
    top = _title(fig, "Strategi vs buy-and-hold (skala log)",
           "Buy & hold: beli di open 1 Jan 2020, tanpa leverage, biaya, atau funding. Area diarsir = OOS.")
    fig.tight_layout(rect=(0, 0, 1, top))
    return _save(fig, "benchmark.png")


def chart_walk_forward(wf: pd.DataFrame) -> str:
    piv = wf.pivot_table(index=["test_start", "segment"], columns="run", values="total_return") * 100
    fig, ax = _figure(figsize=(10, 4))
    n = len(piv)
    width = 0.38
    for j, k in enumerate(["s1", "s2"]):
        xs = [i + (j - 0.5) * (width + 0.02) for i in range(n)]
        ax.bar(xs, piv[k], width=width, color=COLORS[k], label=NAMES[k], linewidth=0)
    ax.axhline(0, color=INK_2, linewidth=0.8)
    ax.set_xticks(range(n))
    ax.set_xticklabels([f"{pd.Timestamp(s):%Y-%m}\n{seg}" for s, seg in piv.index], fontsize=7.5)
    ax.yaxis.set_major_formatter(mtick.PercentFormatter(decimals=1))
    ax.legend(loc="upper left", frameon=False, fontsize=8, labelcolor=INK_2)
    top = _title(fig, "Walk-forward: return per jendela uji 6 bulan",
           "Parameter default di semua jendela; tiap jendela mulai tanpa posisi dan menutup posisi di akhir.")
    fig.tight_layout(rect=(0, 0, 1, top))
    return _save(fig, "walk_forward.png")


def chart_sensitivity(sens: pd.DataFrame) -> str:
    fig, axes = _figure(1, 2, figsize=(10, 4.8))
    for ax, st in zip(axes, ["s1", "s2"]):
        s = sens[sens.strategy == st].iloc[::-1]
        vals = s.total_return * 100
        bars = ax.barh(s.variant, vals, color=COLORS[st], height=0.7, linewidth=0)
        for b, v in zip(bars, s.variant):
            if v == "default":
                b.set_edgecolor(INK)
                b.set_linewidth(1.2)
        ax.axvline(0, color=INK_2, linewidth=0.8)
        ax.grid(axis="x", color=GRID, linewidth=0.6)
        ax.grid(axis="y", visible=False)
        ax.xaxis.set_major_formatter(mtick.PercentFormatter(decimals=1 if vals.abs().max() < 8 else 0))
        ax.set_title(NAMES[st], fontsize=9, color=INK, loc="left")
        ax.tick_params(axis="y", labelsize=7.5)
    top = _title(fig, "Sensitivitas parameter: total return in-sample per varian",
           "Satu parameter diubah per run; batang bergaris tepi = parameter default.")
    fig.tight_layout(rect=(0, 0, 1, top))
    return _save(fig, "sensitivity.png")


def _save(fig, name: str) -> str:
    CHARTS.mkdir(parents=True, exist_ok=True)
    fig.savefig(CHARTS / name, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    return f"charts/{name}"


# ---------------------------------------------------------------- laporan

def read_notes(oos_hash: str) -> str:
    p = REPORTS / "notes.md"
    if not p.exists():
        return "_Belum ada catatan analis (reports/notes.md)._"
    text = p.read_text()
    m = re.search(r"oos_hash:\s*(\w+)", text)
    body = re.sub(r"<!--.*?-->\n?", "", text, flags=re.S).strip()
    if not m or m.group(1) != oos_hash:
        body = ("> ⚠️ Catatan ini ditulis untuk run OOS lain (hash berbeda). "
                "Angka di dalamnya mungkin sudah tidak cocok dengan tabel di bawah.\n\n" + body)
    return body


def build_report() -> str:
    cfg = load_config()
    initial = float(cfg["backtest"]["initial_equity"])
    oos_start = pd.Timestamp(cfg["periods"]["out_of_sample_start"], tz="UTC")
    lock = json.loads((M4 / "oos_lock.json").read_text())
    crit = pd.read_csv(M4 / "criteria.csv")
    m_is = pd.read_csv(M4 / "metrics_is.csv", index_col=0)
    m_oos = pd.read_csv(M4 / "metrics_oos.csv", index_col=0)
    sens = pd.read_csv(M4 / "sensitivity.csv")
    wf = pd.read_csv(M4 / "walk_forward.csv")
    mc = pd.read_csv(M4 / "monte_carlo.csv", index_col=0)
    corr = json.loads((M4 / "summary.json").read_text())["correlation"]

    eq = chained_equity(initial)
    eq.index = pd.to_datetime(eq.index, utc=True)
    bh = {f"buyhold:{asset(s)}": metrics.buy_and_hold(load_bars(s), initial, cfg["data"]["start"])
          for s in cfg["data"]["symbols"]}
    charts = {
        "equity": chart_equity(eq, oos_start, initial),
        "sleeves": chart_sleeves(eq, oos_start, initial),
        "benchmark": chart_benchmark(eq, bh, oos_start),
        "wf": chart_walk_forward(wf),
        "sens": chart_sensitivity(sens),
    }

    c, r, d = cfg["costs"], cfg["risk"], cfg["data"]
    verdict_rows, crit_rows = [], []
    for st in ("s1", "s2"):
        sub = crit[crit.strategy == st]
        ok = sub.lolos.all()
        failed = ", ".join(sub.loc[~sub.lolos, "kriteria"])
        verdict_rows.append([NAMES[st], "**LOLOS**" if ok else "**TIDAK LOLOS**", failed or "-"])
        crit_rows += [[NAMES[st], row.kriteria, row.ambang, row.hasil, "✅" if row.lolos else "❌"]
                      for row in sub.itertuples()]
    any_pass = any(v[1] == "**LOLOS**" for v in verdict_rows)

    sens_rows = [[NAMES[x.strategy], x.variant, fmt("total_return", x.total_return),
                  fmt("profit_factor", x.profit_factor), fmt("max_drawdown", x.max_drawdown),
                  fmt("n_trades", x.n_trades), fmt("total_return", x.return_BTC),
                  fmt("total_return", x.return_ETH)] for x in sens.itertuples()]
    piv = wf.pivot_table(index=["test_start", "test_end", "segment", "partial"], columns="run",
                         values="total_return").reset_index()
    wf_rows = [[f"{x.test_start} s/d {x.test_end}" + (" (parsial)" if x.partial else ""), x.segment,
                fmt("total_return", x.s1), fmt("total_return", x.s2), fmt("total_return", x.portfolio)]
               for x in piv.itertuples()]
    mc_rows = [[lbl, *[fmt("max_drawdown", mc.at[idx, col]) for col in mc.columns]]
               for idx, lbl in [("actual_order", "Urutan asli"), ("median", "Median"),
                                ("p95", "Persentil 95"), ("p99", "Persentil 99"), ("worst", "Terburuk")]]
    last_bar = eq.index[-1].date()

    return f"""# Laporan Fase 1: Riset & Backtest Crypto Futures

Dibuat {pd.Timestamp.now(tz="UTC"):%Y-%m-%d %H:%M UTC} dengan `python -m src.report`.
Out-of-sample dijalankan sekali pada {lock['run_at']} (hash config `{lock['config_hash']}`).
Data harian {d['start']} s/d {last_bar}, sumber Binance USDⓈ-M (endpoint publik).

## Kesimpulan

{"**Minimal satu strategi lolos; boleh lanjut ke fase 2 (paper trading).**" if any_pass else
 "**Tidak ada strategi yang lolos semua kriteria. Menurut PRD, fase 2 (paper trading) belum dimulai.**"}

{md_table(["Strategi", "Status", "Kriteria yang gagal"], verdict_rows, align_right_from=3)}

{md_table(["Strategi", "Kriteria", "Ambang", "Hasil", "Lolos"], crit_rows, align_right_from=5)}

Kriteria dipakai persis seperti di PRD; tidak ada ambang yang diubah setelah melihat hasil.

## Catatan analis

{read_notes(lock['config_hash'])}

## Kurva ekuitas dan drawdown

![Ekuitas dan drawdown]({charts['equity']})

![Ekuitas per sleeve]({charts['sleeves']})

![Benchmark]({charts['benchmark']})

## Metrik out-of-sample ({cfg['periods']['out_of_sample_start']} s/d {last_bar})

Kolom `s1:BTC` dll. adalah run tunggal (risiko 1%/trade); `s1`/`s2` = strategi di dua simbol
(0,5%/trade per sleeve); `portfolio` = empat sleeve (0,25%/trade per sleeve).

{metrics_md(m_oos)}

## Metrik in-sample ({cfg['periods']['in_sample'][0]} s/d {cfg['periods']['in_sample'][1]})

{metrics_md(m_is)}

**Korelasi PnL harian S1 vs S2:** in-sample {corr['is']:.3f}, out-of-sample {corr['oos']:.3f}.

## Sensitivitas parameter (in-sample saja)

![Sensitivitas]({charts['sens']})

{md_table(["Strategi", "Varian", "Return", "Profit factor", "Max DD", "Trade", "Return BTC", "Return ETH"],
          sens_rows, align_right_from=2)}

## Walk-forward

Jendela uji 6 bulan, bergeser tiap 6 bulan, mulai setelah 2 tahun data. Parameter tidak disetel
(PRD melarang optimasi), jadi jendela latih hanya menentukan kapan uji pertama dimulai.

![Walk-forward]({charts['wf']})

{md_table(["Jendela uji", "Segmen", "S1", "S2", "Portofolio"], wf_rows, align_right_from=2)}

## Monte Carlo: urutan trade diacak 1.000 kali (IS + OOS)

{md_table(["Max drawdown", *[NAMES.get(col, col) for col in mc.columns]], mc_rows)}

Angka ini **meremehkan** drawdown sebenarnya: simulasi hanya memakai trade yang sudah ditutup
dan mengabaikan profit belum terealisasi yang hilang di tengah trade serta posisi yang
tumpang-tindih. Contohnya, max drawdown S2 di in-sample sebenarnya
{fmt("max_drawdown", m_is.at["max_drawdown", "s2"])}. Untuk aturan kill switch, pakai drawdown
dari kurva ekuitas, bukan dari tabel ini.

## Drawdown 20% / 30% (bahan kill switch fase 2)

{md_table(["Run", "Pertama DD ≥ 20% (IS)", "Pertama DD ≥ 30% (IS)", "Pertama DD ≥ 20% (OOS)", "Pertama DD ≥ 30% (OOS)"],
          [[col, m_is.at["first_dd_20pct", col], m_is.at["first_dd_30pct", col],
            m_oos.at["first_dd_20pct", col], m_oos.at["first_dd_30pct", col]] for col in m_is.columns],
          align_right_from=1)}

## Setup dan asumsi

| Item | Nilai |
| --- | --- |
| Simbol | {", ".join(d['symbols'])} |
| Eksekusi | sinyal di close hari t, eksekusi di open t+1 |
| Fee taker | {c['fee_taker']:.2%} per sisi |
| Slippage | {c['slippage']:.2%} per sisi, termasuk fill stop |
| Funding | riwayat aktual Binance × qty × open bar tempat event jatuh |
| Risiko per trade | {r['risk_per_trade']:.0%} ekuitas, dibagi rata antar sleeve dalam satu run |
| Batas leverage | {r['max_leverage_per_position']:g}x per posisi, total notional {r['max_total_notional']:g}x ekuitas |
| Cek likuidasi | isolated {r['isolated_leverage']:g}x, MMR {r['maintenance_margin_rate']:.1%}, jarak likuidasi ≥ {r['liq_buffer']:g} × jarak stop |
| Ekuitas awal | {initial:,.0f} |

**Keputusan desain yang diambil selama fase 1** (dikonfirmasi lewat chat kecuali disebut lain):

1. Cek likuidasi memakai leverage isolated tetap 3x dengan buffer 1,5×. Akibatnya stop yang lebih
   jauh dari ~21,9% dari harga entry ditolak. Ini sering terjadi di ETH 2021–2022. Alternatif
   (leverage dipilih per trade) diusulkan tapi tidak diadopsi.
2. Risiko portofolio dibagi rata dari satu ekuitas bersama (1% ÷ jumlah sleeve).
3. Sinyal berlawanan: posisi ditutup di open t+1, posisi baru dibuka di open t+2 tanpa cek ulang.
4. Sisi short S1 aktif (pertanyaan terbuka di PRD, tidak diubah).
5. Asumsi konservatif dari engine, tidak ditanyakan karena PRD meminta versi yang lebih merugikan:
   slippage juga kena di fill stop; stop menang atas exit sinyal di bar yang sama; di bar saat
   stop kena hanya funding yang dibayar yang dihitung, funding yang diterima tidak.
6. Walk-forward tanpa penyetelan parameter (lihat bagian Walk-forward).
7. Indikator memakai smoothing Wilder; data mulai 1 Jan 2020, sehingga S1 (SMA 200) baru bisa
   trading sekitar Juli 2020.

## Pertanyaan terbuka dari PRD

- [ ] Exchange untuk fase 2 dan status izinnya di Indonesia (cek ke OJK).
- [ ] Tarif fee aktual akun, untuk menggantikan 0,05%.
- [ ] Tambah simbol lain (mis. SOL)?
- [ ] Sisi short S1: di data ini rugi di IS dan OOS untuk kedua simbol.
- [ ] Kriteria lolos: perlu ditinjau, terutama syarat ≥100 trade untuk strategi trend harian.
"""


def main() -> None:
    out = REPORTS / "REPORT.md"
    out.write_text(build_report())
    print(f"Laporan: {out.relative_to(ROOT)}")
    for p in sorted(CHARTS.glob("*.png")):
        print(f"Grafik : {p.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

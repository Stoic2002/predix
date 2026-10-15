"""Predix — dashboard read-only untuk robot paper trading dan hasil riset.

    streamlit run app.py

Halaman: Beranda (status robot dalam bahasa sehari-hari), Detail robot, Lab riset, Backtest Fase 1.

Sengaja tidak ada kontrol untuk mengubah parameter strategi: menyetel parameter sambil
melihat hasil (apalagi hasil out-of-sample) melanggar aturan riset di PRD.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import altair as alt
import ccxt
import pandas as pd
import streamlit as st

from src import metrics
from src.data import describe_funding, load_bars, load_config, load_funding, read_meta, validate_ohlcv
from src.report import COLORS, LABELS, NAMES, ROOT, chained_equity, fmt, read_notes
from src import evaluation, live
from src.run import asset
import calendar
from datetime import date

from src.summary import (angka, bot_timeframe, combined_daily_pnl, daily_pnl, display_name, persen, read_db,
                         robot_sections, robot_summary, tanggal)

M4 = ROOT / "reports" / "m4"
UP, DOWN = "#1baf7a", "#e34948"
YEAR_AXIS = alt.Axis(format="%Y", tickCount="year", labelAngle=0)
OOS_FILL = "#8a897f"

alt.data_transformers.disable_max_rows()
APP_NAME = load_config().get("display", {}).get("app_name", "Predix")
st.set_page_config(page_title=APP_NAME, page_icon="📈", layout="wide")


# ---------------------------------------------------------------- data

def label(run: str) -> str:
    """'s1:BTC' -> 'S1 · BTC', 'portfolio' -> 'Portofolio'."""
    if ":" in run and not run.startswith("buyhold"):
        st_, sym = run.split(":")
        return f"{st_.upper()} · {sym}"
    return NAMES.get(run, run)


@st.cache_data
def load_results() -> dict:
    cfg = load_config()
    initial = float(cfg["backtest"]["initial_equity"])
    eq = chained_equity(initial)
    eq.index = pd.to_datetime(eq.index, utc=True)
    bh = pd.DataFrame({f"buyhold:{asset(s)}": metrics.buy_and_hold(load_bars(s), initial, cfg["data"]["start"])
                       for s in cfg["data"]["symbols"]})
    trades = []
    for period in ("is", "oos"):
        t = pd.read_csv(M4 / f"trades_{period}.csv", parse_dates=["signal_time", "entry_time", "exit_time"])
        trades.append(t.assign(period=period.upper()))
    return {
        "cfg": cfg,
        "initial": initial,
        "equity": eq,
        "buyhold": bh.reindex(eq.index),
        "lock": json.loads((M4 / "oos_lock.json").read_text()),
        "criteria": pd.read_csv(M4 / "criteria.csv"),
        "metrics": {p: pd.read_csv(M4 / f"metrics_{p}.csv", index_col=0) for p in ("is", "oos")},
        "sensitivity": pd.read_csv(M4 / "sensitivity.csv"),
        "walk_forward": pd.read_csv(M4 / "walk_forward.csv"),
        "monte_carlo": pd.read_csv(M4 / "monte_carlo.csv", index_col=0),
        "correlation": json.loads((M4 / "summary.json").read_text())["correlation"],
        "trades": pd.concat(trades, ignore_index=True),
    }


@st.cache_data
def bars_for(symbol: str) -> pd.DataFrame:
    return load_bars(symbol)


# ---------------------------------------------------------------- grafik

def oos_band(start, end) -> alt.Chart:
    band = pd.DataFrame({"start": [start], "end": [end], "label": ["out-of-sample"]})
    return alt.Chart(band).mark_rect(color=OOS_FILL, opacity=0.12).encode(
        x="start:T", x2="end:T", tooltip=alt.value(None))


def line_chart(wide: pd.DataFrame, colors: dict[str, str], y_title: str, y_format: str,
               log: bool = False, dashes: dict[str, list[int]] | None = None, height: int = 360,
               x_axis: alt.Axis = YEAR_AXIS) -> alt.Chart:
    """Garis multi-seri dengan crosshair + tooltip berisi semua seri di tanggal itu."""
    names = list(wide.columns)
    long = wide.rename_axis("time").reset_index().melt("time", var_name="name", value_name="value").dropna()
    base = alt.Chart(long).encode(x=alt.X("time:T", title=None, axis=x_axis))
    enc = {
        "y": alt.Y("value:Q", title=y_title, axis=alt.Axis(format=y_format),
                   scale=alt.Scale(type="log" if log else "linear", zero=False)),
        "color": alt.Color("name:N", sort=names, legend=alt.Legend(orient="top", title=None),
                           scale=alt.Scale(domain=names, range=[colors[n] for n in names])),
    }
    if dashes:
        enc["strokeDash"] = alt.StrokeDash("name:N", legend=None, sort=names,
                                           scale=alt.Scale(domain=names, range=[dashes[n] for n in names]))
    lines = base.mark_line(strokeWidth=2).encode(**enc)
    hover = alt.selection_point(fields=["time"], nearest=True, on="pointerover",
                                empty=False, clear="pointerout")
    points = lines.mark_point(filled=True, size=50).encode(
        opacity=alt.condition(hover, alt.value(1), alt.value(0)))
    rule = (alt.Chart(long).transform_pivot("name", value="value", groupby=["time"])
            .mark_rule(color="#8a897f")
            .encode(x="time:T",
                    opacity=alt.condition(hover, alt.value(0.6), alt.value(0)),
                    tooltip=[alt.Tooltip("time:T", title="Tanggal", format="%Y-%m-%d")]
                    + [alt.Tooltip(field=n, type="quantitative", format=y_format) for n in names])
            .add_params(hover))
    return alt.layer(lines, points, rule).properties(height=height)


def candle_chart(bars: pd.DataFrame, trade: pd.Series) -> alt.Chart:
    pad = pd.Timedelta(days=30)
    w = bars[(bars.index >= trade.entry_time - pad) & (bars.index <= trade.exit_time + pad)]
    c = w.rename_axis("time").reset_index()
    size = max(2, int(900 / max(len(c), 1) * 0.6))
    direction = alt.condition("datum.open <= datum.close", alt.value(UP), alt.value(DOWN))
    base = alt.Chart(c).encode(x=alt.X("time:T", title=None, axis=alt.Axis(format="%d %b %Y", labelAngle=0)))
    tip = [alt.Tooltip("time:T", title="Tanggal", format="%Y-%m-%d"),
           *[alt.Tooltip(f"{k}:Q", format=",.2f") for k in ("open", "high", "low", "close")]]
    wick = base.mark_rule().encode(
        y=alt.Y("low:Q", title="Harga", scale=alt.Scale(zero=False)), y2="high:Q", color=direction)
    body = base.mark_bar(size=size).encode(y="open:Q", y2="close:Q", color=direction, tooltip=tip)

    is_long = trade.side == "long"
    marks = pd.DataFrame({
        "time": [trade.entry_time, trade.exit_time],
        "price": [trade.entry_price, trade.exit_price],
        "label": [f"Entry {trade.entry_price:,.2f}", f"Exit ({trade.exit_reason}) {trade.exit_price:,.2f}"],
        "shape": ["triangle-up" if is_long else "triangle-down", "square"],
    })
    pts = alt.Chart(marks).mark_point(filled=True, size=140, color="#2a78d6", stroke="white", strokeWidth=1).encode(
        x="time:T", y="price:Q", shape=alt.Shape("shape:N", scale=None),
        tooltip=[alt.Tooltip("label:N", title="Titik")])
    txt = alt.Chart(marks).mark_text(align="left", dx=9, dy=-9, fontSize=11, color="#a3a29a").encode(
        x="time:T", y="price:Q", text="label:N")
    stop = pd.DataFrame({"time": [trade.entry_time], "time2": [trade.exit_time], "stop": [trade.stop]})
    stop_rule = alt.Chart(stop).mark_rule(strokeDash=[4, 3], color="#8a897f", strokeWidth=1.5).encode(
        x="time:T", x2="time2:T", y="stop:Q", tooltip=[alt.Tooltip("stop:Q", title="Stop", format=",.2f")])
    return alt.layer(wick, body, stop_rule, pts, txt).properties(height=380)


def metrics_frame(m: pd.DataFrame) -> pd.DataFrame:
    rows = {LABELS[k]: {label(c): fmt(k, m.at[k, c]) for c in m.columns} for k in m.index if k in LABELS}
    return pd.DataFrame(rows).T


# ---------------------------------------------------------------- halaman

# ---- Paper trading (Fase 2)
PAPER_DIR = ROOT / "state"


def read_paper(db) -> dict:
    """Baca db paper secara read-only (tidak mengunci db saat bot sedang jalan)."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        get = lambda t: pd.read_sql(f"SELECT * FROM {t}", conn) if t in tables else pd.DataFrame()  # noqa: E731
        return {"meta": dict(conn.execute("SELECT key, value FROM meta").fetchall()),
                **{t: get(t) for t in ("equity", "trades", "fills", "rejections", "runs")}}
    finally:
        conn.close()


def page_robot_detail() -> None:
    st.title("Detail robot")
    full_cfg = load_config()  # tidak di-cache: config bisa berubah tanpa restart
    bot_by_db = {Path(v["db"]).name: (k, v) for k, v in full_cfg.items()
                 if k.startswith("paper") and isinstance(v, dict) and {"strategy", "db"} <= v.keys()}
    dbs = sorted(PAPER_DIR.glob("*.db"))
    if not dbs:
        st.info("Belum ada paper trading. Jalankan `python -m src.paper --all` di terminal.")
    else:
        names = [d.name for d in dbs]
        default = Path(full_cfg["paper"]["db"]).name

        def db_label(n: str) -> str:
            if "demo" in n:
                return "Demo: replay histori (bukan bukti performa)"
            sec, v = bot_by_db.get(n, (None, None))
            if v is None:
                return n
            nm = full_cfg.get("display", {}).get("robots", {}).get(sec, {}).get("name", sec)
            return f"{nm} ({v['strategy'].upper()}, {len(v['symbols'])} koin)"

        pick = st.selectbox("Pilih robot", names, index=names.index(default) if default in names else 0,
                            format_func=db_label)
        # demo dibuat dari bot utama; db yang tidak dikenal juga jatuh ke bot utama
        pcfg = bot_by_db.get(pick, ("paper", full_cfg["paper"]))[1]
        P = read_paper(PAPER_DIR / pick)
        meta, eqp = P["meta"], P["equity"]
        start = pd.Timestamp(meta.get("start", pcfg["start"]), tz="UTC")
        initial = float(pcfg["initial_equity"])
        demo = "demo" in pick
        if demo:
            st.warning(f"DEMO: bot diputar ulang di data historis sejak {start:%Y-%m-%d}. Data ini sudah pernah "
                       "dilihat, jadi BUKAN bukti performa; hanya contoh tampilan. Hasil yang dihitung ada di "
                       "database forward test.")
        st.caption(f"Strategi {pcfg['strategy'].upper()} · {', '.join(s.split('/')[0] for s in pcfg['symbols'])} · mode "
                   f"{meta.get('mode', '-')} · mulai {start:%Y-%m-%d} tanpa posisi · ekuitas virtual {initial:,.0f} · "
                   f"run terakhir {meta.get('last_run', '-')}")
        if eqp.empty:
            first_close = start + pd.Timedelta(days=1)
            st.info(f"Belum ada candle yang diproses. Candle pertama ({start:%Y-%m-%d}) close pada "
                    f"{first_close:%Y-%m-%d} 00:00 UTC (07:00 WIB); bot di VPS memprosesnya otomatis pukul 00:05 UTC (07:05 WIB).")
        else:
            eqp["time"] = pd.to_datetime(eqp["time"], utc=True)
            state = json.loads(meta["state"])
            last_bar = eqp["time"].iloc[-1]
            if not demo:
                behind = (pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=1) - last_bar).days
                if behind > 0:
                    st.warning(f"{behind} candle sudah close tapi belum diproses. Cek timer bot di VPS "
                               "(`journalctl -u crypto-paper.service`).")
            eq_now = eqp["equity"].iloc[-1]
            k = st.columns(5)
            k[0].metric("Ekuitas", f"{eq_now:,.2f}")
            k[1].metric("Return", f"{eq_now / initial - 1:+.2%}")
            k[2].metric("Drawdown", f"{eqp['drawdown'].iloc[-1]:.1%}",
                        help=f"Maksimum sejauh ini {eqp['drawdown'].max():.1%}")
            k[3].metric("Posisi terbuka", int(eqp["n_open"].iloc[-1]))
            k[4].metric("Kill switch", "AKTIF" if state["entries_blocked"] else "tidak aktif",
                        help=f"Peringatan di drawdown {pcfg['kill_switch']['warn_drawdown']:.0%}, "
                             f"stop entry baru di {pcfg['kill_switch']['halt_drawdown']:.0%}.")
            if state["entries_blocked"]:
                st.error("Kill switch aktif: tidak ada entry baru. Reset manual: `python -m src.paper --reset-kill-switch`.")

            nxt = last_bar + pd.Timedelta(days=1)
            c1, c2 = st.columns(2)
            with c1:
                st.markdown(f"**Rencana order di open {nxt:%Y-%m-%d} 00:00 UTC**")
                planned = pd.DataFrame(json.loads(meta.get("planned", "[]")))
                if planned.empty:
                    st.caption("Tidak ada.")
                else:
                    st.dataframe(planned[["symbol", "action", "side", "reason"]], hide_index=True, width="stretch")
            with c2:
                st.markdown(f"**Posisi terbuka (close {last_bar:%Y-%m-%d})**")
                pos = pd.DataFrame(json.loads(meta.get("positions", "[]")))
                if pos.empty:
                    st.caption("Tidak ada.")
                else:
                    pos["entry_time"] = pos["entry_time"].str[:10]
                    st.dataframe(pos[["symbol", "side", "entry_time", "entry_price", "stop", "last_close",
                                      "unrealized", "funding"]], hide_index=True, width="stretch",
                                 column_config={"entry_time": "Sejak", "entry_price": st.column_config.NumberColumn("Entry", format="%.2f"),
                                                "stop": st.column_config.NumberColumn("Stop", format="%.2f"),
                                                "last_close": st.column_config.NumberColumn("Close", format="%.2f"),
                                                "unrealized": st.column_config.NumberColumn("Unrealized", format="%.2f"),
                                                "funding": st.column_config.NumberColumn("Funding", format="%.2f")})

            wide = eqp.set_index("time")[["equity"]].rename(columns={"equity": "Ekuitas paper"})
            st.altair_chart(line_chart(wide, {"Ekuitas paper": COLORS["s2"]}, "Ekuitas ($)", ",.2f", height=280,
                                       x_axis=alt.Axis(format="%d %b %Y", labelAngle=0)),
                            width="stretch")

            tr = P["trades"]
            st.markdown(f"**Trade selesai ({len(tr)})**")
            if tr.empty:
                st.caption("Belum ada trade yang selesai.")
            else:
                tm = metrics.trade_metrics(tr.assign(exit_time=pd.to_datetime(tr.exit_time)))
                st.caption(f"Winrate {fmt('winrate', tm['winrate'])} · profit factor "
                           f"{fmt('profit_factor', tm['profit_factor'])} · expectancy {tm['expectancy_r']:+.2f}R · "
                           f"fee {tm['total_fees']:,.2f} · funding {tm['total_funding']:,.2f}")
                show = tr[["symbol", "side", "entry_time", "exit_time", "entry_price", "exit_price", "exit_reason",
                           "bars_held", "net_pnl", "r_multiple"]].copy()
                for c in ("entry_time", "exit_time"):
                    show[c] = show[c].str[:10]
                st.dataframe(show, hide_index=True, width="stretch")
            with st.expander(f"Log fill ({len(P['fills'])}) dan penolakan ({len(P['rejections'])})"):
                st.dataframe(P["fills"], hide_index=True, width="stretch")
                if not P["rejections"].empty:
                    st.dataframe(P["rejections"], hide_index=True, width="stretch")
        with st.expander(f"Riwayat run bot ({len(P['runs'])})"):
            st.dataframe(P["runs"].iloc[::-1], hide_index=True, width="stretch")


# ---- Lab riset (Fase 2)
LAB_DIR = ROOT / "reports" / "lab"
CHECK_LABELS = {"pass_profit_factor": "PF > 1,2", "pass_trades": "≥ 100 trade", "pass_symbols": "≥ 6/10 simbol profit",
                "pass_sensitivity": "Sensitivitas", "pass_rolling": "Jendela 6 bln", "pass_all": "LOLOS"}

def page_lab() -> None:
    st.title("Lab riset")
    if not (LAB_DIR / "screen.csv").exists():
        st.info("Belum ada hasil lab. Jalankan `python -m src.lab screen`.")
    else:
        rcfg = load_config()["research"]
        smeta = json.loads((LAB_DIR / "screen_meta.json").read_text())
        scr = pd.read_csv(LAB_DIR / "screen.csv")
        st.caption(f"Seleksi {smeta['period'][0]} s/d {smeta['period'][1]} · {len(rcfg['universe'])} simbol · "
                   f"risiko {rcfg['risk_per_trade']:.0%} dibagi rata antar simbol · setelah biaya · "
                   f"run {smeta['run_at']} · {smeta['n_trials_total']} percobaan tercatat di log riset. "
                   "Kandidat & kriteria didaftarkan sebelum hasil dilihat (bagian research di config.yaml).")
        passed = scr[scr.pass_all].candidate.tolist()
        if passed:
            st.success(f"Lolos seleksi: {', '.join(passed)}. Langkah berikutnya: holdout 2024+ (sekali per kandidat).")
        else:
            st.error("Tidak ada kandidat yang lolos seleksi.")
        crit = scr[["candidate", *CHECK_LABELS]].copy()
        for c in CHECK_LABELS:
            crit[c] = crit[c].map({True: "✅", False: "❌"})
        st.markdown("**Kriteria seleksi**")
        st.dataframe(crit.rename(columns={"candidate": "Kandidat", **CHECK_LABELS}), hide_index=True, width="stretch")

        st.markdown("**Hasil seleksi** (portofolio 10 simbol)")
        num = st.column_config.NumberColumn
        st.dataframe(
            scr[["candidate", "total_return", "cagr", "max_drawdown", "sharpe", "profit_factor", "n_trades",
                 "profitable_symbols", "profitable_windows", "deflated_sharpe_prob", "top5_trades_share",
                 "best_symbol", "pf_ex_best_symbol"]],
            hide_index=True, width="stretch",
            column_config={"candidate": "Kandidat", "total_return": num("Return", format="percent"),
                           "cagr": num("CAGR", format="percent"), "max_drawdown": num("Max DD", format="percent"),
                           "sharpe": num("Sharpe", format="%.2f"), "profit_factor": num("PF", format="%.2f"),
                           "n_trades": "Trade", "profitable_symbols": "Simbol profit",
                           "profitable_windows": "Jendela profit",
                           "deflated_sharpe_prob": num("Deflated Sharpe", format="%.2f",
                                                       help="Peluang Sharpe sebenarnya > 0 setelah memperhitungkan jumlah percobaan."),
                           "top5_trades_share": num("Porsi 5 trade terbesar", format="percent"),
                           "best_symbol": "Simbol terbaik",
                           "pf_ex_best_symbol": num("PF tanpa simbol terbaik", format="%.2f")})
        bm = smeta["benchmarks"]
        st.caption("Benchmark periode yang sama (tanpa biaya): " + " · ".join(
            f"{k.replace('_', ' ')}: return {v['total_return']:+.0%}, Sharpe {v['sharpe']:.2f}, max DD {v['max_drawdown']:.0%}"
            for k, v in bm.items()))

        wins = pd.read_csv(LAB_DIR / "screen_windows.csv")
        wins["jendela"] = wins.window_start.str[:7]
        st.markdown("**Return per jendela 6 bulan**")
        st.altair_chart(alt.Chart(wins).mark_rect().encode(
            x=alt.X("jendela:N", title=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("candidate:N", title=None, sort=list(scr.candidate)),
            color=alt.Color("total_return:Q", title="Return", legend=alt.Legend(format=".1%"),
                            scale=alt.Scale(scheme="redyellowgreen", domainMid=0)),
            tooltip=[alt.Tooltip("candidate:N", title="Kandidat"), alt.Tooltip("jendela:N", title="Mulai"),
                     alt.Tooltip("total_return:Q", title="Return", format=".2%"),
                     alt.Tooltip("n_trades:Q", title="Trade")],
        ).properties(height=28 * len(scr)), width="stretch")

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Per simbol** (run tunggal, risiko 1%)")
            sym = pd.read_csv(LAB_DIR / "screen_symbols.csv")
            pick = st.selectbox("Kandidat", list(scr.candidate), key="lab_sym")
            st.dataframe(sym[sym.candidate == pick].drop(columns="candidate"), hide_index=True, width="stretch",
                         column_config={"total_return": num("Return", format="percent"),
                                        "profit_factor": num("PF", format="%.2f")})
        with c2:
            st.markdown("**Sensitivitas** (portofolio)")
            sens_l = pd.read_csv(LAB_DIR / "screen_sensitivity.csv")
            st.dataframe(sens_l[sens_l.candidate == pick].drop(columns="candidate"), hide_index=True, width="stretch",
                         column_config={"total_return": num("Return", format="percent"),
                                        "profit_factor": num("PF", format="%.2f")})
        if (LAB_DIR / "holdout.csv").exists():
            st.markdown("**Holdout 2024+** (dijalankan sekali per kandidat)")
            st.dataframe(pd.read_csv(LAB_DIR / "holdout.csv"), hide_index=True, width="stretch")
        if (LAB_DIR / "fresh_holdout.csv").exists():
            st.markdown("**Ujian koin segar** (10 koin yang tidak pernah dipakai riset, 2020 s/d sekarang, sekali per kandidat)")
            st.dataframe(pd.read_csv(LAB_DIR / "fresh_holdout.csv"), hide_index=True, width="stretch")
        if (LAB_DIR / "intraday_costs_info.csv").exists():
            st.markdown("**Intraday: biaya robot vs biaya paling optimistis** (informasi, bukan kriteria; periode seleksi)")
            st.caption("Optimistis = fee maker Hyperliquid 0,015% dan tanpa slippage, lebih murah dari yang realistis "
                       "(stop loss tetap butuh order market). Kalau ini pun di bawah PF 1,2, strategi tidak layak.")
            ci = pd.read_csv(LAB_DIR / "intraday_costs_info.csv")
            st.dataframe(ci, hide_index=True, width="stretch",
                         column_config={"candidate": "Kandidat", "biaya": "Biaya",
                                        "total_return": num("Return", format="percent"),
                                        "profit_factor": num("PF", format="%.2f"),
                                        "max_drawdown": num("Max DD", format="percent"), "n_trades": "Trade",
                                        "winrate": num("Winrate", format="percent"),
                                        "fees_and_slippage": num("Fee + slippage", format="%.0f"),
                                        "gross_pnl": num("Untung kotor", format="%.0f")})
        hl_section()
        with st.expander("Log riset (semua percobaan)"):
            st.dataframe(pd.read_csv(LAB_DIR / "research_log.csv"), hide_index=True, width="stretch")

def page_phase1() -> None:
    if not (M4 / "oos_lock.json").exists():
        st.error("Hasil validasi Fase 1 belum ada. Jalankan `python -m src.validate`.")
        return
    R = load_results()
    cfg, lock = R["cfg"], R["lock"]
    oos_start = pd.Timestamp(cfg["periods"]["out_of_sample_start"], tz="UTC")
    last_day = R["equity"].index[-1]
    st.title("Backtest Fase 1")
    st.caption("Hasil riset awal (S1 mean reversion & S2 breakout di BTC/ETH). "
               f"OOS dijalankan sekali pada {lock['run_at']} (hash `{lock['config_hash']}`), data "
               f"{cfg['data']['start']} s/d {last_day:%Y-%m-%d}.")
    tab_sum, tab_eq, tab_met, tab_tr, tab_val, tab_data = st.tabs(
        ["Ringkasan", "Ekuitas", "Metrik", "Trade", "Validasi", "Data"])

    # ---- Ringkasan
    with tab_sum:
        crit = R["criteria"]
        passed = {s: crit[crit.strategy == s].lolos.all() for s in ("s1", "s2")}
        if any(passed.values()):
            st.success("Minimal satu strategi lolos semua kriteria: boleh lanjut ke fase 2 (paper trading).")
        else:
            st.error("Tidak ada strategi yang lolos semua kriteria Fase 1. Fase 2 (paper trading S2 + lab riset) "
                     "tetap dimulai atas keputusan pemilik sebagai uji tambahan, bukan karena lolos.")
        m_oos = R["metrics"]["oos"]
        for col, s in zip(st.columns(2), ("s1", "s2")):
            with col, st.container(border=True):
                st.subheader(f"{NAMES[s]}: {'LOLOS ✅' if passed[s] else 'TIDAK LOLOS ❌'}")
                a, b, c = st.columns(3)
                a.metric("Profit factor OOS", fmt("profit_factor", m_oos.at["profit_factor", s]))
                b.metric("Max drawdown OOS", fmt("max_drawdown", m_oos.at["max_drawdown", s]))
                c.metric("Return OOS", fmt("total_return", m_oos.at["total_return", s]))
                sub = crit[crit.strategy == s][["kriteria", "ambang", "hasil", "lolos"]].copy()
                sub["lolos"] = sub["lolos"].map({True: "✅", False: "❌"})
                st.dataframe(sub, hide_index=True, width="stretch",
                             column_config={"kriteria": "Kriteria", "ambang": "Ambang",
                                            "hasil": st.column_config.TextColumn("Hasil", width="medium"),
                                            "lolos": "Lolos"})
        st.caption(f"Korelasi PnL harian S1 vs S2: in-sample {R['correlation']['is']:.3f}, "
                   f"out-of-sample {R['correlation']['oos']:.3f}.")
        with st.expander("Catatan analis", expanded=True):
            st.markdown(read_notes(lock["config_hash"]))

    # ---- Ekuitas
    with tab_eq:
        eq = R["equity"]
        c1, c2 = st.columns([3, 1])
        view = c1.radio("Tampilan", ["Strategi & portofolio", "Per strategi per simbol"], horizontal=True)
        with_bh = c2.toggle("Bandingkan buy & hold", help="Skala log otomatis karena besarnya beda jauh.")
        if view == "Strategi & portofolio":
            cols = ["s1", "s2", "portfolio"]
            wide = eq[cols].rename(columns=NAMES)
            colors = {NAMES[k]: COLORS[k] for k in cols}
            dashes = None
            st.caption("Run per strategi: 0,5%/trade per simbol. Portofolio: 0,25%/trade per sleeve. "
                       "Area abu-abu = out-of-sample.")
        else:
            cols = [c for c in eq.columns if ":" in c]
            wide = eq[cols].rename(columns=label)
            colors = {label(c): COLORS[c.split(":")[0]] for c in cols}
            dashes = {label(c): ([1, 0] if c.endswith("BTC") else [6, 3]) for c in cols}
            st.caption("Tiap seri run terpisah dengan risiko 1%/trade. Garis putus-putus = ETH. "
                       "Area abu-abu = out-of-sample.")
        if with_bh:
            for k in R["buyhold"].columns:
                wide[NAMES[k]] = R["buyhold"][k]
                colors[NAMES[k]] = COLORS[k]
                if dashes is not None:
                    dashes[NAMES[k]] = [1, 0]
        band = oos_band(oos_start, last_day)
        st.altair_chart(alt.layer(band, line_chart(wide, colors, "Ekuitas ($)", ",.0f", log=with_bh, dashes=dashes)),
                        width="stretch")
        dd = wide.apply(metrics.drawdown)
        st.altair_chart(alt.layer(band, line_chart(dd, colors, "Drawdown", ".1%", dashes=dashes, height=220)),
                        width="stretch")

    # ---- Metrik
    with tab_met:
        period = st.radio("Periode", ["Out-of-sample", "In-sample"], horizontal=True)
        key = "oos" if period == "Out-of-sample" else "is"
        rng = (f"{cfg['periods']['out_of_sample_start']} s/d {last_day:%Y-%m-%d}" if key == "oos"
               else " s/d ".join(cfg["periods"]["in_sample"]))
        st.caption(f"{period} {rng}. Kolom S1 · BTC dst. = run tunggal 1%/trade; S1/S2 = dua simbol "
                   "0,5%/trade; Portofolio = empat sleeve 0,25%/trade. Funding positif = dibayar.")
        st.dataframe(metrics_frame(R["metrics"][key]), width="stretch", height=920)

    # ---- Trade
    with tab_tr:
        T = R["trades"]
        runs = [r for r in T.run.unique() if ":" in r] + ["s1", "s2", "portfolio"]
        f1, f2, f3, f4 = st.columns([2, 2, 2, 3])
        run = f1.selectbox("Run", runs, format_func=label, index=runs.index("s2:BTC") if "s2:BTC" in runs else 0)
        per = f2.multiselect("Periode", ["IS", "OOS"], default=["IS", "OOS"])
        side = f3.multiselect("Sisi", ["long", "short"], default=["long", "short"])
        reasons = sorted(T.exit_reason.unique())
        why = f4.multiselect("Alasan exit", reasons, default=reasons)
        sel = T[(T.run == run) & T.period.isin(per) & T.side.isin(side) & T.exit_reason.isin(why)]
        sel = sel.sort_values("entry_time").reset_index(drop=True)

        tm = metrics.trade_metrics(sel) if len(sel) else {"n_trades": 0}
        k = st.columns(5)
        k[0].metric("Trade", tm["n_trades"])
        k[1].metric("Winrate", fmt("winrate", tm.get("winrate", float("nan"))))
        k[2].metric("Profit factor", fmt("profit_factor", tm.get("profit_factor", float("nan"))))
        k[3].metric("Expectancy (R)", fmt("expectancy_r", tm.get("expectancy_r", float("nan"))))
        k[4].metric("PnL bersih ($)", fmt("pnl_long", sel.net_pnl.sum() if len(sel) else 0.0))

        st.caption("Klik satu baris untuk melihat grafik candle trade itu.")
        show = sel[["period", "symbol", "side", "entry_time", "exit_time", "entry_price", "exit_price", "stop",
                    "exit_reason", "bars_held", "net_pnl", "r_multiple", "fees", "funding"]]
        num = lambda title, f: st.column_config.NumberColumn(title, format=f)  # noqa: E731
        event = st.dataframe(
            show, hide_index=True, width="stretch", height=320, on_select="rerun", selection_mode="single-row",
            column_config={
                "period": "Periode", "symbol": "Simbol", "side": "Sisi",
                "entry_time": st.column_config.DateColumn("Entry", format="YYYY-MM-DD"),
                "exit_time": st.column_config.DateColumn("Exit", format="YYYY-MM-DD"),
                "entry_price": num("Harga entry", "%.2f"), "exit_price": num("Harga exit", "%.2f"),
                "stop": num("Stop", "%.2f"),
                "exit_reason": "Alasan exit", "bars_held": "Hari",
                "net_pnl": num("PnL bersih", "%.2f"), "r_multiple": num("R", "%.2f"),
                "fees": num("Fee", "%.2f"), "funding": num("Funding", "%.2f"),
            })
        rows = event.selection.rows if event and event.selection else []
        if rows:
            t = sel.iloc[rows[0]]
            st.markdown(f"**{t.symbol} · {t.side} · {t.entry_time:%Y-%m-%d} → {t.exit_time:%Y-%m-%d}** · "
                        f"PnL bersih {t.net_pnl:,.2f} ({t.r_multiple:+.2f}R) · fee {t.fees:,.2f} · "
                        f"funding {t.funding:,.2f} · {t.bars_held} hari")
            st.altair_chart(candle_chart(bars_for(t.symbol), t), width="stretch")
            st.caption("Segitiga = entry, kotak = exit, garis putus-putus = stop. Rentang ±30 hari di sekitar trade.")

    # ---- Validasi
    with tab_val:
        st.subheader("Sensitivitas parameter (in-sample)")
        sens = R["sensitivity"].copy()
        sens["variant"] = sens.variant.replace("default", "default (dipakai) ◆")
        for col, s in zip(st.columns(2), ("s1", "s2")):
            d = sens[sens.strategy == s]
            chart = alt.Chart(d).mark_bar(color=COLORS[s], cornerRadiusEnd=4).encode(
                y=alt.Y("variant:N", sort=list(d.variant), title=None, axis=alt.Axis(labelOverlap=False)),
                x=alt.X("total_return:Q", title="Total return IS", axis=alt.Axis(format=".1%")),
                tooltip=[alt.Tooltip("variant:N", title="Varian"),
                         alt.Tooltip("total_return:Q", title="Return", format=".1%"),
                         alt.Tooltip("profit_factor:Q", title="Profit factor", format=".2f"),
                         alt.Tooltip("max_drawdown:Q", title="Max DD", format=".1%"),
                         alt.Tooltip("n_trades:Q", title="Trade")],
            ).properties(height=30 * len(sens[sens.strategy == "s1"]), title=NAMES[s])
            rule = alt.Chart(pd.DataFrame({"x": [0]})).mark_rule(color="#52514e").encode(x="x:Q")
            col.altair_chart(alt.layer(chart, rule), width="stretch")
        st.caption("Satu parameter diubah per run; ◆ = parameter default yang dipakai di semua run lain.")

        st.subheader("Walk-forward (jendela uji 6 bulan)")
        wf = R["walk_forward"]
        wf = wf[wf.run.isin(["s1", "s2"])].assign(
            jendela=lambda x: x.test_start.str[2:7] + " " + x.segment, nama=lambda x: x.run.map(NAMES))
        st.altair_chart(alt.Chart(wf).mark_bar(cornerRadiusEnd=4).encode(
            x=alt.X("jendela:N", title=None, sort=list(dict.fromkeys(wf.jendela)),
                    axis=alt.Axis(labelAngle=0, labelOverlap=False)),
            xOffset=alt.XOffset("nama:N", sort=[NAMES["s1"], NAMES["s2"]]),
            y=alt.Y("total_return:Q", title="Return per jendela", axis=alt.Axis(format=".1%")),
            color=alt.Color("nama:N", title=None, legend=alt.Legend(orient="top"),
                            scale=alt.Scale(domain=[NAMES["s1"], NAMES["s2"]], range=[COLORS["s1"], COLORS["s2"]])),
            tooltip=[alt.Tooltip("jendela:N", title="Jendela"), alt.Tooltip("nama:N", title="Strategi"),
                     alt.Tooltip("total_return:Q", title="Return", format=".2%"),
                     alt.Tooltip("n_trades:Q", title="Trade")],
        ).properties(height=320), width="stretch")
        st.caption("Parameter default di semua jendela; tiap jendela mulai tanpa posisi dan menutup posisi di akhir.")

        st.subheader("Monte Carlo: urutan trade diacak 1.000×")
        mc = R["monte_carlo"].rename(columns=NAMES, index={
            "actual_order": "Urutan asli", "median": "Median", "p95": "Persentil 95",
            "p99": "Persentil 99", "worst": "Terburuk"})
        st.dataframe(mc.map(lambda v: f"{v:.1%}"), width="stretch")
        st.warning("Angka Monte Carlo meremehkan drawdown sebenarnya (hanya trade yang sudah ditutup, "
                   "tanpa profit belum terealisasi yang hilang). Untuk kill switch, pakai drawdown dari tab Ekuitas.")

    # ---- Data
    with tab_data:
        d = cfg["data"]
        meta = read_meta(ROOT / d["dir"])
        st.caption(f"Sumber: {meta.get('exchange', '-')} · update terakhir {meta.get('last_update_utc', '-')}. "
                   "Update data lewat terminal: `python -m src.data`. Data baru tidak mengubah hasil validasi "
                   "sampai `src.validate` dijalankan ulang.")
        for col, sym in zip(st.columns(len(d["symbols"])), d["symbols"]):
            with col, st.container(border=True):
                b, f = load_bars(sym, d), load_funding(sym, d)
                st.subheader(sym)
                a, c = st.columns(2)
                a.metric("Candle harian", f"{len(b):,}", help=f"{b.index[0]:%Y-%m-%d} s/d {b.index[-1]:%Y-%m-%d}")
                c.metric("Event funding", f"{len(f):,}", help=f"{f.index[0]:%Y-%m-%d} s/d {f.index[-1]:%Y-%m-%d}")
                raw = b.assign(timestamp=b.index.as_unit("ms").asi8).reset_index(drop=True)
                issues = validate_ohlcv(raw, d["timeframe"], d["start"])
                raw_f = f.assign(timestamp=f.index.as_unit("ms").asi8).reset_index(drop=True)
                info, f_issues = describe_funding(raw_f)
                if issues or f_issues:
                    for msg in issues + f_issues:
                        st.warning(msg)
                else:
                    st.success("Validasi OK: tidak ada candle hilang, high/low konsisten, tidak ada nilai ≤ 0.")
                for msg in info:
                    st.caption(msg)


HL_DIR = ROOT / "reports" / "hyperliquid"


def hl_section() -> None:
    """Ringkasan riset trader top Hyperliquid (data on-chain publik, nama samaran)."""
    if not (HL_DIR / "traders.csv").exists():
        return
    st.divider()
    st.subheader("Riset: trader top Hyperliquid")
    meta = json.loads((HL_DIR / "meta.json").read_text())
    d = pd.read_csv(HL_DIR / "traders.csv")
    st.caption("Data on-chain publik bursa Hyperliquid, diambil 14 Okt 2026. Wallet anonim; laporan memakai nama samaran. "
               "Keterbatasan: hanya pemenang yang terlihat (akun yang habis tidak ada di leaderboard), data transaksi "
               "terbatas 10.000 terakhir per akun, dan kita melihat apa yang dilakukan, bukan kenapa.")
    k = st.columns(4)
    k[0].metric("Akun di leaderboard", f"{meta['leaderboard_accounts']:,}".replace(",", "."))
    k[1].metric("200 profit terbesar (modal ≥ $100rb)", meta["candidates"])
    k[2].metric(f"Konsisten ({meta['consistent'] / meta['candidates']:.0%} dari 200)", meta["consistent"],
                help="Untung di minimal 60% bulan, riwayat minimal 6 bulan.")
    k[3].metric("Dianalisis", len(d))
    g = (d.groupby("style").agg(jumlah=("trader", "count"), transaksi_per_hari=("fills_per_day", "median"),
                               market_order=("taker_share", "median"), lama_pegang_jam=("hold_median_h", "median"),
                               winrate=("winrate", "median"), profit_factor=("profit_factor", "median"),
                               leverage_sekarang=("exposure_now", "median"))
         .sort_values("jumlah", ascending=False).reset_index())
    num = st.column_config.NumberColumn
    st.markdown("**Gaya trading pemenang konsisten**")
    st.dataframe(g, hide_index=True, width="stretch", column_config={
        "style": "Gaya", "jumlah": "Jumlah", "transaksi_per_hari": num("Transaksi/hari", format="%.0f"),
        "market_order": num("Porsi market order", format="percent"),
        "lama_pegang_jam": num("Lama pegang (jam, median)", format="%.1f"),
        "winrate": num("Winrate", format="percent"), "profit_factor": num("Profit factor", format="%.2f"),
        "leverage_sekarang": num("Eksposur saat ini (× modal)", format="%.1f")})
    hft = d["style"].str.contains("HFT|market maker").sum()
    direc = d[d["style"].str.startswith(("intraday", "swing", "posisi ("))]
    st.markdown(
        f"- **{hft} dari {len(d)} ({hft / len(d):.0%}) adalah HFT/market maker**: ribuan transaksi per hari, keunggulan dari "
        "kecepatan, fee super rendah, dan modal besar. Tidak bisa ditiru dengan VPS biasa.\n"
        f"- **Trader directional ({len(direc)} akun) mirip robot kita**: winrate median "
        f"{direc['winrate'].median():.0%}, untung rata-rata sekitar {direc['win_loss_ratio'].median():.1f}× rugi rata-rata. "
        "Lebih sering rugi kecil, menang dari sedikit trade besar.\n"
        f"- **Spesialis**: 3 koin teratas mencakup median {d['top3_share'].median():.0%} volume mereka "
        "(banyak di HYPE dan pasar khusus Hyperliquid).\n"
        f"- **Leverage cukup tinggi** (median eksposur {d['exposure_now'].median():.1f}× modal), tapi ini hanya yang selamat; "
        "akun ber-leverage tinggi yang habis tidak terlihat.\n"
        "- **Pemenang pun punya periode buruk**: beberapa trader swing/intraday rugi di jendela data terakhirnya "
        "walau profit sepanjang masanya puluhan juta dolar.")
    with st.expander("Trader directional (intraday, swing, posisi)"):
        st.dataframe(direc[["trader", "style", "hold_median_h", "episodes", "winrate", "profit_factor", "win_loss_ratio",
                            "long_share", "top_coin", "top3_share", "exposure_now", "profitable_months"]],
                     hide_index=True, width="stretch", column_config={
                         "hold_median_h": num("Lama pegang (jam)", format="%.1f"), "winrate": num("Winrate", format="percent"),
                         "profit_factor": num("PF", format="%.2f"), "win_loss_ratio": num("Untung/rugi rata-rata", format="%.1f"),
                         "long_share": num("Porsi long", format="percent"), "top3_share": num("Porsi 3 koin teratas", format="percent"),
                         "exposure_now": num("Eksposur (×)", format="%.1f"),
                         "profitable_months": num("Bulan untung", format="percent")})


# ---------------------------------------------------------------- Live (perkiraan sementara)

@st.cache_resource(show_spinner=False)
def _exchange():
    ex = ccxt.binanceusdm({"enableRateLimit": True})
    ex.load_markets()
    return ex


@st.cache_data(ttl=10, show_spinner=False)
def _live_candles(symbols: tuple, candle_iso: str, timeframe: str) -> tuple[dict, str]:
    t = pd.Timestamp(candle_iso)
    return live.fetch_candles(_exchange(), list(symbols), t, timeframe), pd.Timestamp.now(tz="UTC").isoformat()


@st.cache_data(ttl=600, show_spinner=False)
def _live_funding(symbols: tuple, candle_iso: str, timeframe: str) -> dict:
    return live.fetch_funding(_exchange(), list(symbols), pd.Timestamp(candle_iso), timeframe)


def _pos_table(positions: list[dict]):
    df = pd.DataFrame([{
        "Koin": p["symbol"].split("/")[0], "Sisi": "beli" if p["side"] == "long" else "short",
        "Sejak": "hari ini" if p["today_entry"] else tanggal(p["entry_time"])[:-5],
        "Harga beli": p["entry_price"], "Sekarang": p["last_close"],
        "Untung/rugi": p["unrealized"], "%": p["change"], "Jarak ke stop": p["to_stop"],
    } for p in positions])
    color = lambda v: f"color: {PROFIT}" if v > 0 else f"color: {LOSS}" if v < 0 else ""  # noqa: E731
    return (df.style
            .format({"Harga beli": lambda v: angka(v, 2 if v >= 1 else 4), "Sekarang": lambda v: angka(v, 2 if v >= 1 else 4),
                     "Untung/rugi": _signed, "%": persen, "Jarak ke stop": lambda v: angka(v * 100, 1) + "%"})
            .map(color, subset=["Untung/rugi", "%"]))


LIVE_REFRESH_SECONDS = 15


@st.fragment(run_every=LIVE_REFRESH_SECONDS)
def live_panel(cfg_now: dict) -> None:
    sections = [s for s in robot_sections(cfg_now) if live.next_candle(cfg_now, s) is not None]
    st.subheader("🔴 Live")
    if not sections:
        st.caption("Panel live aktif setelah robot mulai bekerja.")
        return
    groups: dict[tuple, list[str]] = {}
    for s in sections:  # robot harian & 4 jam punya candle berjalan yang berbeda
        groups.setdefault((bot_timeframe(cfg_now, s), live.next_candle(cfg_now, s)), []).append(s)
    snaps, fetched_at = {}, None
    now = pd.Timestamp.now(tz="UTC")
    for (tf, t), secs in list(groups.items()):
        if t > now:  # robot baru: candle pertamanya belum dimulai
            for s in secs:
                snaps[s] = {"ok": False, "reason": f"Mulai bekerja dari candle {tanggal(t, True)}. "
                                                   "Belum ada yang bisa ditampilkan."}
            del groups[(tf, t)]
    try:
        for (tf, t), secs in groups.items():
            syms = tuple(sorted({sym for s in secs for sym in cfg_now[s]["symbols"]}))
            candles, fetched_at = _live_candles(syms, t.isoformat(), tf)
            funding = _live_funding(syms, t.isoformat(), tf)
            for s in secs:
                snaps[s] = live.live_snapshot(cfg_now, s, candles, funding, t)
    except Exception as e:  # noqa: BLE001  jaringan/exchange: panel live opsional, Beranda tetap jalan
        st.warning(f"Harga live tidak bisa diambil saat ini ({type(e).__name__}). Dicoba lagi otomatis.")
        return
    if fetched_at is None:
        st.caption("Belum ada robot yang candle-nya sedang berjalan.")
    else:
        jam = pd.Timestamp(fetched_at).tz_convert("Asia/Jakarta").strftime("%H:%M:%S")
        st.caption(f"🔄 Otomatis tiap {LIVE_REFRESH_SECONDS} detik · harga Binance pukul **{jam} WIB**. "
                   "**Perkiraan sementara**. "
                   "Dihitung dengan mesin yang sama dengan robot pada candle yang sedang berjalan; angka resmi "
                   "dicatat saat candle itu selesai (robot harian: besok 07:05 WIB; robot 4 jam: 5 menit setelah "
                   "candle 4 jam tutup).")
    ok = {s: v for s, v in snaps.items() if v["ok"]}
    if len(ok) > 1:
        total = sum(v["today_pnl"] for v in ok.values())
        base = sum(v["equity_before"] for v in ok.values())
        st.metric("Gabungan hari ini (sejauh ini)", _signed(total), persen(total / base) if total else None)
    for col, sec in zip(st.columns(len(sections)), sections):
        snap = snaps[sec]
        with col, st.container(border=True):
            st.markdown(f"**{display_name(cfg_now, sec)[0]}**")
            if not snap["ok"]:
                st.info(snap["reason"])
                continue
            st.metric("Modal sekarang (perkiraan)", angka(snap["equity_now"], 0),
                      f"{_signed(snap['today_pnl'])} hari ini ({persen(snap['today_pct'])})"
                      if abs(snap["today_pnl"]) >= 0.005 else None)
            for t in snap["stops_today"]:
                st.warning(f"Stop loss {t['symbol'].split('/')[0]} tersentuh hari ini di sekitar "
                           f"{angka(t['exit_price'])} (rugi {angka(abs(t['net_pnl']))}).")
            if snap["positions"]:
                st.dataframe(_pos_table(snap["positions"]), hide_index=True, width="stretch")
            else:
                st.caption("Tidak memegang posisi.")
            for r in snap["rejected_today"]:
                koin = r["symbol"].split("/")[0]
                if "likuidasi" in r["reason"]:
                    why = (f"stop loss-nya harus {angka(r['stop_pct'] * 100, 1)}% dari harga karena sedang sangat "
                           "bergejolak, terlalu dekat dengan harga likuidasi (aturan keamanan, sama seperti di backtest)")
                elif "kill switch" in r["reason"]:
                    why = "kill switch aktif"
                else:
                    why = r["reason"]
                st.caption(f"⚪ Beli {koin} dibatalkan: {why}.")


# ---------------------------------------------------------------- Radar & evaluasi

@st.cache_data(ttl=6 * 3600, show_spinner="Menghitung pembanding backtest...")
def _reference(section: str, settings: str) -> dict:  # settings = hash setting robot, kunci cache
    return evaluation.reference(load_config(), section)


def _radar_prices(cfg_now: dict, sections: list[str]) -> dict[str, float]:
    """Harga live dari candle yang sedang berjalan (cache sama dengan panel live); kosong kalau gagal."""
    prices: dict[str, float] = {}
    for sec in sections:
        t = live.next_candle(cfg_now, sec)
        if t is None or t > pd.Timestamp.now(tz="UTC"):
            continue
        try:
            candles, _ = _live_candles(tuple(sorted(cfg_now[sec]["symbols"])), t.isoformat(), bot_timeframe(cfg_now, sec))
        except Exception:  # noqa: BLE001  pakai close terakhir
            continue
        prices.update({f"{sec}|{sym}": c[3] for sym, c in candles.items()})
    return prices


def _pct(v) -> str:
    return "-" if v is None or pd.isna(v) else ("+" if v > 0 else "") + angka(v * 100, 1) + "%"


@st.fragment(run_every=LIVE_REFRESH_SECONDS)
def radar_panel(cfg_now: dict) -> None:
    st.subheader("📡 Radar sinyal")
    st.caption("Seberapa jauh harga sekarang dari pemicu berikutnya. Pemicu dicek saat candle **tutup** "
               "(robot harian 07:00 WIB, Robot 3 tiap 4 jam); stop loss bisa kena kapan saja. "
               "Level dihitung dengan rumus yang sama dengan robot dan bisa bergeser tiap candle.")
    sections = robot_sections(cfg_now)
    prices = _radar_prices(cfg_now, sections)
    for tab, sec in zip(st.tabs([display_name(cfg_now, s)[0] for s in sections]), sections):
        with tab:
            try:
                p = {k.split("|", 1)[1]: v for k, v in prices.items() if k.startswith(sec + "|")}
                df = evaluation.radar(cfg_now, sec, p)
            except Exception as e:  # noqa: BLE001
                st.info(f"Radar belum tersedia untuk robot ini ({e}).")
                continue
            strat, params = evaluation.radar_strategy(cfg_now, sec)
            st.caption(f"Aturan: {evaluation.rule_text(strat, params)}. "
                       f"Harga: {'live' if p else 'close terakhir'}.")
            near = df[df["jarak"].abs() < 0.02]
            if len(near):
                st.warning("Dekat pemicu (< 2%): " + ", ".join(f"{r.koin} ({r['aksi berikutnya']})"
                                                             for _, r in near.iterrows()))
            fmt_px = lambda v: "-" if v is None or pd.isna(v) else angka(v, 2 if v >= 1 else 4)  # noqa: E731
            show = pd.DataFrame({
                "Koin": df["koin"], "Status": df["status"], "Pemicu berikutnya": df["aksi berikutnya"],
                "Level": df["level"].map(fmt_px), "Harga": df["harga"].map(fmt_px), "Jarak": df["jarak"].map(_pct),
                "Stop loss": df["stop loss"].map(fmt_px),
                "Jarak ke stop": [(_pct(s / h - 1) if s is not None and not pd.isna(s) else "-")
                                  for s, h in zip(df["stop loss"], df["harga"])],
                "Catatan": df["catatan"]})
            st.dataframe(show, hide_index=True, width="stretch")


def evaluation_panel(cfg_now: dict) -> None:
    from src.paper import settings_hash
    ev_cfg = cfg_now.get("evaluation")
    if not ev_cfg:
        return
    st.subheader("📊 Evaluasi vs backtest")
    st.caption(f"Apakah hasil paper masih wajar dibanding uji historis robot yang sama "
               f"({tanggal(pd.Timestamp(ev_cfg['reference_start']))} sampai sehari sebelum robot mulai). "
               f"Aturan penilaian ditetapkan 14 Okt 2026 sebelum ada hasil, dan putusan akhir keluar setelah "
               f"{ev_cfg['verdict_after_days']} hari. 🟢 wajar · 🟡 waspada · 🔴 di luar batas · ⏳ belum cukup data.")
    sections = robot_sections(cfg_now)
    for col, sec in zip(st.columns(len(sections)), sections):
        with col, st.container(border=True):
            st.markdown(f"**{display_name(cfg_now, sec)[0]}**")
            try:
                ref = _reference(sec, settings_hash(cfg_now, sec))
                e = evaluation.evaluate(cfg_now, sec, ref)
            except Exception as ex:  # noqa: BLE001
                st.info(f"Evaluasi belum tersedia ({type(ex).__name__}: {ex}).")
                continue
            text, status = e["verdict"]
            {"ok": st.success, "warn": st.warning, "fail": st.error}.get(status, st.info)(
                f"{evaluation.ICON[status]} {text}")
            st.markdown("\n".join(f"- {evaluation.ICON[c['status']]} **{c['aspek']}**: {c['nilai']}  \n"
                                   f"  <small>{c['patokan']}{'. ' + c['keterangan'] if c['keterangan'] else ''}</small>"
                                   for c in e["checks"]), unsafe_allow_html=True)
            r = e["ref"]
            idle = angka(e["idle_days"], 0 if e["idle_days"] >= 10 else 1)
            since = "transaksi terakhir" if e["has_fill"] else "mulai, belum pernah transaksi"
            st.caption(f"⏱️ Sudah {idle} hari sejak {since}. Di backtest, jarak antar entry biasanya "
                       f"{angka(r['median_gap_days'], 0 if r['median_gap_days'] >= 10 else 1)} hari dan robot pernah "
                       f"tanpa posisi sama sekali selama {angka(r['longest_idle_days'], 0)} hari, jadi diam lama itu normal.")


# ---------------------------------------------------------------- Beranda

STATUS_BOX = {"ok": st.success, "wait": st.info, "late": st.warning, "error": st.error}


def page_home() -> None:
    cfg_now = load_config()  # tidak di-cache: status robot harus selalu terbaru
    st.title(APP_NAME)
    st.caption("Robot trading yang berjalan otomatis di server: robot harian bekerja pukul **07:05 WIB**, "
               "Robot 3 setiap **4 jam**. Masih **simulasi**: uang virtual, tidak ada order sungguhan ke exchange.")
    live_panel(cfg_now)
    radar_panel(cfg_now)
    st.subheader("Status robot")
    sections = robot_sections(cfg_now)
    for col, sec in zip(st.columns(len(sections)), sections):
        s = robot_summary(cfg_now, sec)
        with col, st.container(border=True):
            st.subheader(s["name"])
            st.caption(f"{s['about']} Koin: {', '.join(s['coins'])}.")
            STATUS_BOX[s["status"]](s["status_text"])
            if s["kill_switch"]:
                st.error("⛔ Kill switch aktif: drawdown terlalu dalam, robot berhenti membeli sampai di-reset.")
            m = st.columns(3)
            m[0].metric("Modal (virtual)", angka(s["equity"], 0),
                        f"{persen(s['change'])} sejak {s['start']}" if abs(s["change"]) >= 0.0005 else None)
            m[1].metric("Posisi dipegang", len(s["positions"]))
            m[2].metric("Trade selesai", s["n_trades"])
            if s["decision_day"]:
                st.markdown(f"**Keputusan hari ini** ({s['decision_day']})")
                st.markdown("\n".join(f"- {o}" for o in s["orders"]) if s["orders"]
                            else "Tidak ada. Robot menunggu sinyal.")
            if s["positions"]:
                st.markdown("**Sedang dipegang**")
                st.markdown("\n".join(f"- {p}" for p in s["positions"]))
            if s["recent_trades"]:
                st.markdown("**Trade terakhir yang selesai**")
                st.markdown("\n".join(f"- {t}" for t in s["recent_trades"]))
            if s["notes"]:
                st.warning(s["notes"])

    evaluation_panel(cfg_now)
    with st.expander("Bagaimana robot ini bekerja?"):
        st.markdown(
            "- **Sekali sehari (Robot 1 & 2).** Binance menutup candle harian pukul 07:00 WIB. Pukul 07:05 robot "
            "membaca harga kemarin, lalu memutuskan beli, jual, atau diam. Robot 3 (eksperimen) memakai aturan Robot 1 "
            "di candle 4 jam dan bekerja tiap 4 jam. Strategi diuji dengan data harian, jadi robot harus "
            "memutuskan tepat setelah candle tutup; jam lain membuat hasilnya tidak bisa dibandingkan.\n"
            "- **Jarang trading.** Keduanya robot tren: posisi biasa dipegang berminggu-minggu. Hari tanpa aksi itu normal.\n"
            "- **Simulasi.** Harga, biaya, dan funding mengikuti data asli, tapi uangnya virtual (10.000 per robot) "
            "dan tidak ada order ke exchange. Tujuannya membuktikan apakah hasil nyata mirip hasil uji historis.\n"
            "- **Pengaman.** Setiap posisi punya stop loss. Kalau modal turun 30% dari puncaknya, robot berhenti "
            "membeli (kill switch) sampai di-reset manual.\n"
            "- **Berapa lama?** Butuh minimal 3–6 bulan sebelum hasilnya cukup untuk dinilai."
        )


# ---------------------------------------------------------------- Kalender untung/rugi

HARI = ["Sen", "Sel", "Rab", "Kam", "Jum", "Sab", "Min"]
BULAN_PANJANG = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September",
                 "Oktober", "November", "Desember"]
PROFIT, LOSS, FLAT = "#1baf7a", "#e34948", "#8a897f"


def _signed(x: float, desimal: int = 2) -> str:
    return ("+" if x > 0 else "−" if x < 0 else "") + angka(abs(x), desimal)


def calendar_chart(data: pd.DataFrame, year: int, month: int) -> alt.Chart:
    """Kalender satu bulan: kotak per hari, hijau = untung, merah = rugi, pekat = besar."""
    by_day = {r.date: r for r in data.itertuples()}
    first_wd = date(year, month, 1).weekday()
    vals = [abs(r.pnl) for r in data.itertuples() if r.date.year == year and r.date.month == month]
    m = max(vals) if vals and max(vals) > 0 else 1.0
    rows = []
    for d in range(1, calendar.monthrange(year, month)[1] + 1):
        dt = date(year, month, d)
        r = by_day.get(dt)
        has = r is not None
        pnl = float(r.pnl) if has else 0.0
        strength = (0.18 + 0.82 * abs(pnl) / m) if has and pnl != 0 else (0.12 if has else 0.04)
        rows.append({
            "tanggal": tanggal(dt), "day": str(d), "hari": HARI[dt.weekday()], "week": (d + first_wd - 1) // 7,
            "fill": PROFIT if pnl > 0 else LOSS if pnl < 0 else FLAT, "strength": strength,
            "ink": "#ffffff" if strength > 0.55 else FLAT,
            "nilai": (_signed(pnl, 0 if abs(pnl) >= 100 else 1) if has else ""),
            "untung_rugi": (_signed(pnl) if has else "belum ada data"),
            "persen": (persen(r.pct) if has else "-"),
            "trade": (r.trades or "-") if has else "-",
        })
    df = pd.DataFrame(rows)
    n_weeks = int(df.week.max()) + 1
    base = alt.Chart(df).encode(
        x=alt.X("hari:O", sort=HARI, title=None, axis=alt.Axis(orient="top", labelAngle=0, ticks=False, domain=False,
                                                               labelFontSize=12, labelPadding=8)),
        y=alt.Y("week:O", title=None, axis=None))
    tip = [alt.Tooltip("tanggal:N", title="Tanggal"), alt.Tooltip("untung_rugi:N", title="Untung/rugi"),
           alt.Tooltip("persen:N", title="Persen"), alt.Tooltip("trade:N", title="Trade ditutup")]
    cell = base.mark_rect(cornerRadius=6, width=alt.RelativeBandSize(0.93), height=alt.RelativeBandSize(0.9)).encode(
        color=alt.Color("fill:N", scale=None), opacity=alt.Opacity("strength:Q", scale=None), tooltip=tip)
    day = base.mark_text(dy=-11, fontSize=11).encode(text="day:N", color=alt.Color("ink:N", scale=None), tooltip=tip)
    val = base.mark_text(dy=9, fontSize=13, fontWeight="bold").encode(
        text="nilai:N", color=alt.Color("ink:N", scale=None), tooltip=tip)
    return alt.layer(cell, day, val).properties(height=66 * n_weeks)


def month_bars(data: pd.DataFrame) -> alt.Chart:
    g = data.assign(bulan=pd.to_datetime(data["date"]).dt.to_period("M").astype(str)).groupby("bulan")["pnl"].sum()
    df = g.reset_index().assign(
        warna=lambda x: x.pnl.map(lambda v: PROFIT if v >= 0 else LOSS),
        label=lambda x: x.pnl.map(_signed),
        nama=lambda x: x.bulan.map(lambda b: f"{BULAN_PANJANG[int(b[5:]) - 1][:3]} {b[:4]}"))
    base = alt.Chart(df).encode(x=alt.X("nama:N", sort=list(df.nama), title=None, axis=alt.Axis(labelAngle=0)))
    bars = base.mark_bar(cornerRadiusEnd=4).encode(
        y=alt.Y("pnl:Q", title="Untung/rugi ($)"), color=alt.Color("warna:N", scale=None),
        tooltip=[alt.Tooltip("nama:N", title="Bulan"), alt.Tooltip("label:N", title="Untung/rugi")])
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color=FLAT).encode(y="y:Q")
    return alt.layer(bars, zero).properties(height=220)


def page_calendar() -> None:
    cfg_now = load_config()
    st.title("Kalender untung/rugi")
    st.caption("Untung/rugi harian = perubahan modal virtual dari kemarin ke hari itu, termasuk posisi yang masih "
               "terbuka, fee, dan funding. Tanggal = hari candle; hasilnya tercatat pukul 07:05 WIB keesokan harinya. "
               "Arahkan kursor ke kotak untuk detail.")
    options = {"Gabungan semua robot": None}
    for sec in robot_sections(cfg_now):
        options[display_name(cfg_now, sec)[0]] = sec
    demo = ROOT / "state" / "paper_demo.db"
    if demo.exists():
        options["Demo (replay histori, bukan bukti)"] = "__demo__"
    pick = st.radio("Robot", list(options), horizontal=True, label_visibility="collapsed")
    sec = options[pick]
    if sec is None:
        data, initial = combined_daily_pnl(cfg_now)
    elif sec == "__demo__":
        initial = float(cfg_now["paper"]["initial_equity"])
        data = daily_pnl(read_db(demo), initial)
    else:
        initial = float(cfg_now[sec]["initial_equity"])
        data = daily_pnl(read_db(ROOT / cfg_now[sec]["db"]), initial)

    if data.empty:
        st.info("Belum ada hari yang tercatat. Kalender mulai terisi setelah robot bekerja pertama kali "
                "(besok pukul 07:05 WIB).")
        return

    months = sorted({(d.year, d.month) for d in data["date"]}, reverse=True)
    y, mth = st.selectbox("Bulan", months, format_func=lambda ym: f"{BULAN_PANJANG[ym[1] - 1]} {ym[0]}")
    md = data[[d.year == y and d.month == mth for d in data["date"]]]
    total = md["pnl"].sum()
    start_eq = float(md["equity"].iloc[0] - md["pnl"].iloc[0])
    best, worst = md.loc[md["pnl"].idxmax()], md.loc[md["pnl"].idxmin()]
    c1, c2 = st.columns([1, 2])
    c1.metric(f"Total {BULAN_PANJANG[mth - 1]}", _signed(total), persen(total / start_eq) if total else None)
    c2.markdown(
        f"**{(md.pnl > 0).sum()}** hari untung · **{(md.pnl < 0).sum()}** hari rugi · {(md.pnl == 0).sum()} hari datar  \n"
        f"Terbaik: **{_signed(best.pnl)}** ({tanggal(best.date)[:-5]})  \n"
        f"Terburuk: **{_signed(worst.pnl)}** ({tanggal(worst.date)[:-5]})")
    st.altair_chart(calendar_chart(data, y, mth), width="stretch")

    if len(months) > 1:
        st.markdown("**Per bulan**")
        st.altair_chart(month_bars(data), width="stretch")


# ---------------------------------------------------------------- navigasi

nav = st.navigation({
    APP_NAME: [st.Page(page_home, title="Beranda", icon="🏠", default=True),
               st.Page(page_calendar, title="Kalender", icon="📅", url_path="kalender"),
               st.Page(page_robot_detail, title="Detail robot", icon="📊", url_path="robot")],
    "Riset": [st.Page(page_lab, title="Lab riset", icon="🧪", url_path="lab"),
              st.Page(page_phase1, title="Backtest Fase 1", icon="📜", url_path="fase1")],
})
nav.run()

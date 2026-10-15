# Predix: trend-following crypto robots (research, backtesting, paper trading)

Predix is a research pipeline and a set of paper-trading robots for crypto perpetual futures on
Binance (USDⓈ-M). Everything runs in **simulation**: virtual money, public market data, no API keys
and no orders sent to any exchange.

- **Phase 1:** offline research on BTC/ETH daily candles, with an honest pass/fail report
  ([reports/REPORT.md](reports/REPORT.md)).
- **Phase 2:** a research lab with pre-registered candidates, three paper-trading robots running on a
  VPS, a Streamlit dashboard, and daily Telegram reports.

## Robots (paper trading, dry-run)

| Robot | Config section | Rules | Symbols | Timeframe |
| --- | --- | --- | --- | --- |
| Robot 1 · Breakout | `paper` | Donchian 55/20, long & short, 3×ATR stop | BTC, ETH | 1d |
| Robot 2 · Momentum | `paper_tsmom` | 60-day time-series momentum, long only | 10 coins | 1d |
| Robot 3 · Breakout 4h (experiment) | `paper_h4` | Robot 1 rules on 4-hour candles | BTC, ETH | 4h |

All robots use the same execution engine as the backtests (`src/backtest.py`), so paper results are
directly comparable with research results. This parity is covered by tests (`tests/test_paper.py`,
`tests/test_live.py`, `tests/test_h4_robot.py`). Each robot locks its settings: if its strategy, cost or
risk parameters change, it refuses to run so that results are never mixed across settings.

Risk rules: risk per trade is 1% of equity split evenly across a robot's symbols, max 2× leverage per
position, max 3× total notional, isolated-margin liquidation check, and a kill switch (warning at 20%
drawdown, new entries blocked at 30% until a manual reset).

## Setup

```bash
python -m venv .venv            # Python 3.14
source .venv/bin/activate
pip install -r requirements.txt
```

## Commands

| Task | Command | Output |
| --- | --- | --- |
| Fetch/update daily data and funding, validate | `python -m src.data` | `data/*.csv` |
| Validate data only (no network) | `python -m src.data --validate-only` | terminal summary |
| Unit tests | `python -m pytest -q` | |
| Phase 1: strategies on the in-sample period | `python -m src.run` | `reports/m3_insample/` |
| Phase 1: validation (sensitivity, walk-forward, one-shot OOS) | `python -m src.validate` | `reports/m4/` |
| Phase 1: report and charts | `python -m src.report` | `reports/REPORT.md`, `reports/charts/` |
| Lab: screen pre-registered candidates | `python -m src.lab screen` | `reports/lab/` |
| Lab: one-shot time holdout (2024+) | `python -m src.lab holdout NAME ...` | `reports/lab/holdout.csv` |
| Lab: one-shot fresh-coin holdout | `python -m src.lab fresh NAME ...` | `reports/lab/fresh_holdout.csv` |
| Stress test 2017–2019 (incl. 2018 crash) | `python -m src.pre2020` | `reports/pre2020/` |
| Gold & silver test (2000–now) | `python -m src.metals` | `reports/metals/` |
| 4-hour vs daily test | `python -m src.h4` | `reports/h4/` |
| Intraday research: fetch 1h candles | `python -m src.intraday fetch` | `data/*_1h.csv` |
| Intraday research: optimistic-cost info run | `python -m src.intraday costs` | `reports/lab/intraday_costs_info.csv` |
| Run all paper robots | `python -m src.paper --all` | `state/*.db` |
| Dashboard | `streamlit run app.py` | http://localhost:8501 |

## Research protocol

- Candidates, parameters, sensitivity variants and pass criteria are written in `config.yaml`
  (`research` section) **before** results are seen.
- Selection uses 2020–2023 only. Each candidate gets one time holdout (2024 onwards) and, if it passes,
  one test on 10 coins never used by the research (`research.fresh_holdout`). Both are locked per
  candidate (`reports/lab/*_lock.json`).
- Every run is logged in `reports/lab/research_log.csv`. The number of trials feeds the Deflated Sharpe
  Ratio, so the bar rises as more ideas are tried.
- Phase 1 out-of-sample results are locked to a config hash (`reports/m4/oos_lock.json`).
  `src.validate` refuses to re-run with a changed config unless `--allow-oos-rerun` is given.

Main findings so far: mean reversion (RSI-2) has no edge after costs. Trend following passes on crypto,
including the 2018 crash and untouched coins. Short sides add almost nothing. Gold/silver trend
following fails on Binance perpetuals because of funding costs. Intraday strategies on 1h candles
(opening-range breakout, intraday momentum, intraday mean reversion; flat by 00:00 UTC) all fail
selection: costs exceed the small gross edge, and even with optimistic maker fees and no slippage none
reaches PF 1.2.

## Paper trading

```bash
python -m src.paper --all                 # all robots, sequentially
python -m src.paper --bot paper_tsmom     # one robot
python -m src.paper --no-fetch            # use local CSVs, no network
python -m src.paper --reset-kill-switch   # allow new entries after a kill switch
python -m src.paper --db state/paper_demo.db --start 2026-01-01   # demo replay, not evidence
```

Runs are idempotent: candles already processed are skipped, and missed candles (e.g. after downtime)
are processed in order.

## Dashboard (read-only)

```bash
streamlit run app.py
```

Pages:

- **Home:** each robot in plain language, plus a **Live** panel that re-runs the engine on the
  current, unfinished candle every 15 seconds (an estimate; official numbers are recorded when the candle
  closes). Below it, a **signal radar** shows how far each coin is from its next trigger (same formulas as
  the strategies, covered by tests), and an **evaluation card** compares each robot's paper results with
  its own backtest using rules fixed in `config.yaml` (`evaluation`) before any paper results existed.
- **Calendar:** daily profit/loss per robot or combined, with monthly totals.
- **Robot detail:** equity, positions, trades, fills and run log.
- **Research lab** and **Phase 1 backtest** pages.

The dashboard never changes parameters or writes to the robots' databases.

## Telegram reports

A full report is sent once a day after the daily robots run, and also when a robot fails or falls
behind. Robot 3 only sends a short alert when it actually trades. One-time setup (the token is typed
hidden and stored in `.env` with mode 600):

```bash
python -m src.notify --setup
python -m src.notify --test
```

The dashboard link in reports is read from `DASHBOARD_URL` in `.env`.

## Deployment (VPS)

The VPS address, dashboard domain and SSH key name are intentionally not in this repository; keep
them in a local, git-ignored `deploy/LOCAL.md`. Placeholders used below: `<user>@<IP-VPS>`,
`~/.ssh/<ssh-key>`, `https://<dashboard-subdomain>`.

The robots run **only** on the VPS (`~/crypto-bot`, Python 3.14.7 via `uv`). Don't run `src.paper`
on a laptop as well, or the two copies' results will diverge.

- `deploy/crypto-paper.timer`: runs `python -m src.paper --all` every 4 hours at :05 UTC. Daily robots
  only act at 00:05 UTC; the other runs double as retries.
- `deploy/crypto-dashboard.service`: Streamlit on `127.0.0.1:8502`, published through Caddy with
  basic auth (`deploy/caddy-bot-block.caddy`, Cloudflare-proxied DNS, Let's Encrypt certificate).
- Set or change the dashboard password:
  `ssh -t -i ~/.ssh/<ssh-key> <user>@<IP-VPS> 'sudo bash ~/crypto-bot/deploy/set-dashboard-password.sh'`
- Bot logs: `ssh -i ~/.ssh/<ssh-key> <user>@<IP-VPS> 'journalctl -u crypto-paper.service -n 30 -o cat'`
- Deploy code: rsync the project to the VPS excluding `.venv`, `state/`, `data/`, `.env` and
  `deploy/LOCAL.md`, then `sudo systemctl restart crypto-dashboard.service`.

## Project layout

```
config.yaml              all parameters: data, costs, risk, strategies, research, robots, display
src/data.py              Binance public data: fetch, incremental update, validation, loaders
src/indicators.py        SMA, RSI, ATR (Wilder), Donchian
src/strategies/          pure signal functions (data in, signals out) + registry
src/costs.py             fees, slippage, funding
src/sizing.py            risk-based sizing, leverage caps, liquidation check
src/backtest.py          bar-by-bar engine (resumable state machine); execution assumptions in docstring
src/metrics.py           performance metrics
src/run.py, validate.py, report.py   Phase 1 pipeline
src/lab.py               research lab (screen, holdout, fresh holdout, deflated Sharpe)
src/pre2020.py, metals.py, h4.py     one-shot extra tests
src/intraday.py          1h data fetch and cost scenarios for intraday candidates
src/paper.py             paper-trading robots (daily and 4h)
src/live.py              live estimate on the unfinished candle
src/summary.py           plain-language summaries, daily P&L, Telegram text
src/evaluation.py        signal radar and paper-vs-backtest evaluation (dashboard, read-only)
src/notify.py            Telegram reports
app.py                   Streamlit dashboard
deploy/                  systemd units, Caddy block, password script
reports/                 research outputs (CSV, charts, locks)
tests/                   unit and parity tests
```

Strategies know nothing about balances or orders. A live bot only needs to call
`generate_signals()` on the latest closed candle.

## Disclaimer

Research and simulation only. Nothing here is investment advice, and backtest or paper results do not
guarantee future returns.

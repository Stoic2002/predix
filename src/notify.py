"""Laporan Telegram harian untuk robot paper trading.

    python -m src.notify --setup   # sekali saja: masukkan token bot (tersembunyi), deteksi chat id, kirim pesan uji
    python -m src.notify --test    # kirim pesan uji
    python -m src.notify           # kirim laporan sekarang

Token dan chat id disimpan di file .env di root proyek (izin 600), tidak pernah di config.yaml atau log.
Laporan otomatis dikirim oleh `python -m src.paper --all` setelah robot jalan.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from src.data import ROOT, load_config
from src.summary import is_daily, robot_sections, robot_summary, telegram_text, trade_alert_text

ENV = ROOT / ".env"
API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4000  # batas Telegram 4096 karakter per pesan


# ---------------------------------------------------------------- .env

def load_env(path: Path = ENV) -> dict[str, str]:
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def save_env(values: dict[str, str], path: Path = ENV) -> None:
    env = {**load_env(path), **values}
    path.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    os.chmod(path, 0o600)


# ---------------------------------------------------------------- API

class TelegramError(RuntimeError):
    pass


def call(token: str, method: str, params: dict | None = None) -> dict:
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(API.format(token=token, method=method), data=data)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            desc = json.loads(e.read()).get("description", "")
        except Exception:  # noqa: BLE001
            desc = ""
        raise TelegramError(f"Telegram menolak ({e.code}) {desc}".strip()) from None
    except (urllib.error.URLError, TimeoutError) as e:
        # jangan sertakan URL: berisi token
        raise TelegramError(f"tidak bisa menghubungi Telegram: {type(e).__name__}") from None
    if not body.get("ok"):
        raise TelegramError(f"Telegram menolak: {body.get('description', 'tanpa keterangan')}")
    return body["result"]


def split_message(text: str, limit: int = MAX_LEN) -> list[str]:
    parts, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > limit and cur:
            parts.append(cur)
            cur = ""
        cur += line
    return parts + ([cur] if cur else [])


def send(text: str, env: dict | None = None) -> None:
    env = env if env is not None else load_env()
    for part in split_message(text):
        call(env["TELEGRAM_BOT_TOKEN"], "sendMessage",
             {"chat_id": env["TELEGRAM_CHAT_ID"], "text": part, "disable_web_page_preview": "true"})


def configured(env: dict) -> bool:
    return bool(env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"))


# ---------------------------------------------------------------- dipanggil setelah robot jalan

def should_send(processed: dict[str, int], failed: list[str], statuses: list[str]) -> bool:
    """Kirim kalau ada candle baru, ada robot gagal, atau ada robot terlambat.
    Run cadangan siang hari yang tidak memproses apa-apa tidak mengirim pesan dobel."""
    return any(n > 0 for n in processed.values()) or bool(failed) or "late" in statuses


def after_run(cfg: dict, processed: dict[str, int], failed: list[str], env: dict | None = None,
              results: dict | None = None) -> None:
    """Laporan lengkap: sekali sehari (robot harian memproses candle baru) atau saat ada masalah.
    Robot intraday (mis. 4 jam): pesan singkat hanya kalau bertransaksi, supaya Telegram tidak berisik."""
    env = env if env is not None else load_env()
    if not configured(env):
        print("[notify] Telegram belum diatur (python -m src.notify --setup); laporan tidak dikirim.")
        return
    sections = robot_sections(cfg)
    daily = [s for s in sections if is_daily(cfg, s)]
    intraday = [s for s in sections if not is_daily(cfg, s)]
    statuses = [robot_summary(cfg, s)["status"] for s in sections]
    msgs = []
    if should_send({s: processed.get(s, 0) for s in daily}, failed, statuses):
        msgs.append(telegram_text(cfg, failed))
    alert_text = trade_alert_text(cfg, results or {}, intraday)
    if alert_text:
        msgs.append(alert_text)
    if not msgs:
        print("[notify] tidak ada laporan atau transaksi baru; tidak ada pesan.")
        return
    try:
        for m in msgs:
            send(m, env)
        print(f"[notify] {len(msgs)} pesan Telegram terkirim.")
    except TelegramError as e:  # laporan gagal tidak boleh menggagalkan robot
        print(f"[notify] gagal kirim Telegram: {e}")


def alert(text: str) -> None:
    """Peringatan singkat (mis. data gagal diambil). Diam kalau Telegram belum diatur."""
    env = load_env()
    if configured(env):
        try:
            send(text, env)
        except TelegramError as e:
            print(f"[notify] gagal kirim peringatan: {e}")


# ---------------------------------------------------------------- setup

def setup() -> None:
    print("Setup laporan Telegram.\n"
          "1) Di Telegram, chat @BotFather -> /newbot, ikuti langkahnya, salin token yang diberikan.\n"
          "2) Buka bot barumu di Telegram dan kirim pesan apa saja (mis. 'halo').\n")
    token = getpass.getpass("Tempel token bot (tidak akan terlihat): ").strip()
    if ":" not in token:
        raise SystemExit("Format token tidak dikenali (seharusnya angka:huruf). Tidak ada yang disimpan.")
    me = call(token, "getMe")
    print(f"Token valid: bot @{me['username']}")
    updates = call(token, "getUpdates")
    chats = {}
    for u in updates:
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            chats[chat["id"]] = chat.get("first_name") or chat.get("title") or chat.get("username") or ""
    if not chats:
        raise SystemExit(f"Belum ada pesan ke @{me['username']}. Kirim 'halo' ke bot itu di Telegram, "
                         "lalu jalankan setup ini lagi. Tidak ada yang disimpan.")
    if len(chats) > 1:
        for cid, nm in chats.items():
            print(f"  chat id {cid}: {nm}")
        chat_id = input("Lebih dari satu chat ditemukan. Ketik chat id tujuan laporan: ").strip()
    else:
        chat_id = str(next(iter(chats)))
        print(f"Chat ditemukan: {next(iter(chats.values()))}")
    save_env({"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat_id})
    send("✅ Predix terhubung. Laporan harian akan dikirim setiap pagi sekitar 07:05 WIB.")
    print(f"Tersimpan di {ENV} (izin 600). Pesan uji terkirim; cek Telegram.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    if args.setup:
        setup()
        return
    env = load_env()
    if not configured(env):
        raise SystemExit("Telegram belum diatur. Jalankan: python -m src.notify --setup")
    send("✅ Pesan uji dari Predix." if args.test else telegram_text(load_config()), env)
    print("Terkirim.")


if __name__ == "__main__":
    main()

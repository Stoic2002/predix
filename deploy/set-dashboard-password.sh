#!/usr/bin/env bash
# Set atau ganti login dashboard crypto-bot (basic auth di Caddy). Jalankan di VPS:
#   sudo bash ~/crypto-bot/deploy/set-dashboard-password.sh
# Password tidak pernah ditulis ke disk atau muncul di daftar proses; hanya hash bcrypt yang disimpan.
set -euo pipefail

AUTH_FILE=/etc/caddy/crypto-bot-auth.caddy

[[ $EUID -eq 0 ]] || { echo "Jalankan dengan sudo."; exit 1; }

read -rp "Username dashboard [arul]: " user
user=${user:-arul}
[[ "$user" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "Username hanya boleh huruf, angka, titik, minus, garis bawah."; exit 1; }
read -rsp "Password baru (minimal 12 karakter): " pw; echo
read -rsp "Ulangi password: " pw2; echo
[[ "$pw" == "$pw2" ]] || { echo "Password tidak sama. Tidak ada yang diubah."; exit 1; }
(( ${#pw} >= 12 )) || { echo "Password minimal 12 karakter. Tidak ada yang diubah."; exit 1; }

hash=$(printf '%s\n' "$pw" | caddy hash-password)
unset pw pw2
[[ "$hash" == \$2* ]] || { echo "Gagal membuat hash. Tidak ada yang diubah."; exit 1; }

tmp=$(mktemp)
printf 'basic_auth {\n\t%s %s\n}\n' "$user" "$hash" > "$tmp"
install -m 640 -o root -g caddy "$tmp" "$AUTH_FILE"
rm -f "$tmp"
echo "Login disimpan di $AUTH_FILE (hanya hash)."

# Kalau blok dashboard sudah ada di Caddyfile, terapkan langsung tanpa downtime.
if grep -q "crypto-bot-auth.caddy" /etc/caddy/Caddyfile; then
    caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
    systemctl reload caddy
    echo "Caddy di-reload; password baru langsung berlaku."
fi

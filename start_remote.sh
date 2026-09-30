#!/bin/bash
# Перезапуск freyd-remote: TLS-сервер + legacy-мост (plain HTTP для bootstrap).
# Канал клиента: https://5.129.212.74:8443 (прямой IP, туннель больше не нужен).
set -u
cd "$(dirname "$0")"

# 1. Останавливаем старое
pkill -f "python3 server.py" 2>/dev/null
pkill -f "cloudflared tunnel --url http://127.0.0.1:8093" 2>/dev/null
sleep 1

# 2. Сервер (TLS :8443 + legacy :8093)
setsid nohup python3 server.py >> remote/server.log 2>/dev/null < /dev/null &
sleep 2

# 3. Проверка
if python3 remote/ctl.py --status >/dev/null 2>&1; then
    echo "OK"
else
    echo "!! Сервер не отвечает, см. remote/server.log"
    exit 1
fi

#!/usr/bin/env python3
"""Freyd remote command server.

Схема: клиент (ПК Саши) сам стучается сюда по HTTP,
 забирает команды из очереди, выполняет и отдаёт вывод обратно.

Эндпоинты:
  GET  /api/poll?wait=25   — клиент ждёт команду (long-poll)
  POST /api/result         — клиент присылает вывод {id, output, exit_code}
  POST /api/cmd            — я кладу команду в очередь {cmd}
  GET  /api/status         — состояние: очередь, онлайн клиента, результаты
  GET  /api/results        — последние результаты
  GET  /client.ps1         — скачать Windows-клиент
  GET  /client.py          — скачать Python-клиент

Зависимости: только стандартная библиотека.
Запуск: python3 server.py   (порт FREYD_PORT, по умолчанию 8093)
"""
import hashlib
import json
import os
import re
import secrets
import ssl
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE = Path(__file__).resolve().parent
REMOTE_DIR = BASE / "remote"
TOKEN_FILE = REMOTE_DIR / "token.txt"
LEGACY_TOKEN_FILE = REMOTE_DIR / "legacy_token.txt"
STATE_FILE = REMOTE_DIR / "state.json"
PORT = int(os.environ.get("FREYD_PORT", "8093"))       # legacy-мост (plain HTTP, только bootstrap)
TLS_PORT = int(os.environ.get("FREYD_TLS_PORT", "8443"))  # рабочий канал — TLS с пиннингом
PUB_HOST = os.environ.get("FREYD_HOST", "5.129.212.74")
CERT_FILE = REMOTE_DIR / "cert.pem"
KEY_FILE = REMOTE_DIR / "key.pem"
MAX_RESULTS = 200
MAX_BODY = 10 * 1024 * 1024

REMOTE_DIR.mkdir(exist_ok=True)
if TOKEN_FILE.exists():
    TOKEN = TOKEN_FILE.read_text().strip()
else:
    TOKEN = secrets.token_hex(16)
    TOKEN_FILE.write_text(TOKEN + "\n")
    TOKEN_FILE.chmod(0o600)
LEGACY_TOKEN = (LEGACY_TOKEN_FILE.read_text().strip()
                if LEGACY_TOKEN_FILE.exists() else "")

_lock = threading.Lock()
_cond = threading.Condition(_lock)
_queue = []      # [{"id": int, "cmd": str, "ts": float}]
_results = []    # [{"id": int, "cmd": str, "output": str, "exit": int|None, "ts": float}]
_seq = 0
_client_seen = 0.0  # время последнего /api/poll от клиента
_last_notify = {}   # text -> ts, дедуп уведомлений


def _load_state():
    global _seq, _queue, _results
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            _seq = int(data.get("seq", 0))
            _queue = list(data.get("queue", []))
            _results = list(data.get("results", []))[-MAX_RESULTS:]
        except Exception:
            pass


def _save_state():
    try:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"seq": _seq, "queue": _queue, "results": _results[-MAX_RESULTS:]},
            ensure_ascii=False))
        tmp.replace(STATE_FILE)
    except Exception:
        pass


def _json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _sha256_file(path):
    """SHA256 файла — hex в верхнем регистре (формат как у Get-FileHash)."""
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _update_ps1():
    """Скрипт обновления: подменяет клиентские файлы и перезапускает клиент."""
    exp_client = _sha256_file(REMOTE_DIR / "client.ps1")
    exp_notify = _sha256_file(REMOTE_DIR / "notify.ps1")
    return (
        "# Freyd updater (генерируется сервером)\r\n"
        "$dir = \"$env:LOCALAPPDATA\\Freyd\"\r\n"
        "Start-Sleep -Seconds 2\r\n"
        "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" |\r\n"
        "    Where-Object { $_.CommandLine -like \"*Freyd\\client.ps1*\" -and $_.ProcessId -ne $PID } |\r\n"
        "    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }\r\n"
        "Start-Sleep -Seconds 1\r\n"
        "if ((Test-Path \"$dir\\client.ps1.new\") -and ((Get-FileHash \"$dir\\client.ps1.new\" -Algorithm SHA256).Hash -eq \"%s\")) {\r\n"
        "    Move-Item -Force \"$dir\\client.ps1.new\" \"$dir\\client.ps1\"\r\n"
        "}\r\n"
        "if ((Test-Path \"$dir\\notify.ps1.new\") -and ((Get-FileHash \"$dir\\notify.ps1.new\" -Algorithm SHA256).Hash -eq \"%s\")) {\r\n"
        "    Move-Item -Force \"$dir\\notify.ps1.new\" \"$dir\\notify.ps1\"\r\n"
        "}\r\n"
        "Start-Process powershell.exe -ArgumentList \"-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `\"$dir\\client.ps1`\"\" -WindowStyle Hidden\r\n"
    ) % (exp_client, exp_notify)


def _bootstrap_cmd():
    """Команда для legacy-клиента (plain HTTP): стянуть TLS-клиент и обновиться.

    Защита от MITM в переходный момент — сверка хэшей внутри update.ps1.
    """
    base = "https://%s:%d" % (PUB_HOST, TLS_PORT)
    d = "%LOCALAPPDATA%\\Freyd"
    return (
        'curl -sk -o %s\\client.ps1.new %s/client.ps1?token=%s & '
        'curl -sk -o %s\\notify.ps1.new %s/notify.ps1?token=%s & '
        'curl -sk -o %s\\update.ps1 %s/update.ps1?token=%s & '
        'powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File %s\\update.ps1'
    ) % (d, base, TOKEN, d, base, TOKEN, d, base, TOKEN, d)


def _notify_owner(text):
    """Шлёт событие с ПК Саши прямо ему в Telegram (через bot API)."""
    try:
        bot_token = None
        chat_id = None
        for line in (BASE / ".env").read_text().splitlines():
            if line.startswith("BOT_TOKEN="):
                bot_token = line.split("=", 1)[1].strip()
            elif line.startswith("ALLOWED_USER_ID="):
                chat_id = line.split("=", 1)[1].strip()
        if not bot_token or not chat_id:
            print("[notify] нет BOT_TOKEN/ALLOWED_USER_ID в .env", flush=True)
            return
        payload = json.dumps({"chat_id": chat_id, "text": "🖥 " + text}).encode()
        req = urllib.request.Request(
            "https://api.telegram.org/bot%s/sendMessage" % bot_token,
            data=payload, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            ok = json.loads(r.read().decode())
        print("[notify] telegram ok=%s" % ok.get("ok"), flush=True)
    except Exception as e:
        print("[notify] telegram error: %s" % e, flush=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FreydRemote/1.0"

    # ---------- helpers ----------
    def _send(self, code, obj, ctype="application/json; charset=utf-8"):
        body = obj if isinstance(obj, bytes) else _json_bytes(obj)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _qs(self):
        return parse_qs(urlparse(self.path).query)

    def _authorized(self, qs, allow_legacy=False):
        # Токен обязателен всегда. Старый (ротированный) токен действует ТОЛЬКО
        # на legacy-порту и только для выдачи bootstrap-обновления.
        tok = self.headers.get("X-Token") or (qs.get("token") or [""])[0]
        if tok == TOKEN:
            return True
        return (allow_legacy and self.server.server_address[1] == PORT
                and LEGACY_TOKEN and tok == LEGACY_TOKEN)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BODY:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def log_message(self, fmt, *args):
        # long-poll молчит, остальное логируем
        if "/api/poll" not in (args[0] if args else ""):
            print("[%s] %s" % (time.strftime("%H:%M:%S"), fmt % args), flush=True)

    # ---------- GET ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/update.ps1":
            if not self._authorized(qs):
                return self._send(403, {"error": "bad token"})
            return self._send(200, _update_ps1().encode("utf-8"),
                              "text/plain; charset=utf-8")

        if path in ("/client.ps1", "/client.py", "/install.ps1", "/notify.ps1", "/dbg.ps1"):
            if not self._authorized(qs):
                return self._send(403, {"error": "bad token"})
            f = REMOTE_DIR / path.lstrip("/")
            if f.exists():
                return self._send(200, f.read_bytes(), "text/plain; charset=utf-8")
            return self._send(404, {"error": "not found"})

        if not self._authorized(qs, allow_legacy=(path == "/api/poll")):
            return self._send(403, {"error": "bad token"})

        if path == "/api/poll":
            return self._poll(qs)
        if path == "/api/client_meta":
            return self._client_meta(qs)
        if path == "/api/status":
            return self._send(200, self._status())
        if path == "/api/results":
            limit = min(int((qs.get("limit") or ["20"])[0]), MAX_RESULTS)
            with _lock:
                items = list(_results[-limit:])
            return self._send(200, {"results": items})
        if path == "/":
            return self._send(200, {
                "service": "freyd-remote", "queue": len(_queue),
                "client_online": self._client_online()})
        return self._send(404, {"error": "unknown endpoint"})

    def _client_meta(self, qs):
        """Самообновление клиента: сравниваем хэши файлов на ПК с серверными."""
        want_client = _sha256_file(REMOTE_DIR / "client.ps1")
        want_notify = _sha256_file(REMOTE_DIR / "notify.ps1")
        have_client = ((qs.get("h_client") or [""])[0] or "").upper()
        have_notify = ((qs.get("h_notify") or [""])[0] or "").upper()
        update = (have_client != want_client) or (have_notify != want_notify)
        return self._send(200, {
            "update": update,
            "sha_client": want_client,
            "sha_notify": want_notify,
        })

    def _poll(self, qs):
        global _client_seen
        # Legacy-мост (plain HTTP): клиент со старой версией — сразу отдаём
        # команду самообновления на TLS-клиент, очередь не трогаем.
        if self.server.server_address[1] == PORT:
            print("[poll] legacy-клиент -> отдаю bootstrap-обновление", flush=True)
            return self._send(200, {"id": 0, "cmd": _bootstrap_cmd()})
        try:
            wait = float((qs.get("wait") or ["20"])[0])
        except ValueError:
            wait = 20.0
        wait = max(0.0, min(wait, 60.0))
        deadline = time.time() + wait
        with _cond:
            _client_seen = time.time()
            while not _queue:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                _cond.wait(remaining)
            if _queue:
                item = _queue.pop(0)
                _save_state()
                return self._send(200, {"id": item["id"], "cmd": item["cmd"]})
        return self._send(200, {"id": None, "cmd": None})

    def _client_online(self):
        return (_client_seen > 0) and (time.time() - _client_seen < 60)

    def _status(self):
        with _lock:
            return {
                "queue": len(_queue),
                "client_online": self._client_online(),
                "last_seen": round(time.time() - _client_seen, 1) if _client_seen else None,
                "results": list(_results[-10:]),
            }

    # ---------- POST ----------
    def do_POST(self):
        path = urlparse(self.path).path
        qs = self._qs()
        if not self._authorized(qs):
            return self._send(403, {"error": "bad token"})
        data = self._body()

        if path == "/api/cmd":
            cmd = (data.get("cmd") or "").strip()
            if not cmd:
                return self._send(400, {"error": "empty cmd"})
            global _seq
            with _cond:
                _seq += 1
                item = {"id": _seq, "cmd": cmd, "ts": time.time()}
                _queue.append(item)
                _save_state()
                _cond.notify_all()
            print("[cmd #%s] %s" % (item["id"], cmd), flush=True)
            return self._send(200, {"id": item["id"]})

        if path == "/api/notify":
            # ПК прислал событие (выключение/перезагрузка/старт)
            data_text = (data.get("text") or "").strip()
            msg = (data.get("msg") or "").strip()
            if not data_text and not msg:
                return self._send(400, {"error": "empty text"})
            # Вердикт принимаем ЗДЕСЬ: текст события на разных ОС может быть
            # на любом языке, python с re.IGNORECASE разбирает надёжнее.
            # На Win Саши строка приходит в cp866-мойке (байты UTF-8,
            # растянутые через OEM-кодировку) — чиним encode/decode-ом.
            text = data_text
            if msg:
                variants = [msg]
                for enc in ("cp866", "cp1251"):
                    try:
                        variants.append(msg.encode(enc, errors="strict").decode("utf-8"))
                    except Exception:
                        pass
                hit = any(
                    re.search(r"перезагруз|перезапу|restart|reboot", v, re.I)
                    for v in variants
                )
                text = "перезагружаюсь..." if hit else "выключаюсь..."
            if not text:
                return self._send(400, {"error": "empty text"})
            # дедуп: одинаковый текст повторно в течение 45 с не шлём
            now = time.time()
            if msg:
                print("[notify] raw msg: %r" % msg[:180], flush=True)
            with _lock:
                if now - _last_notify.get(text, 0) < 45:
                    return self._send(200, {"ok": True, "dup": True})
                _last_notify[text] = now
                for k in [k for k, t in _last_notify.items() if now - t > 300]:
                    _last_notify.pop(k, None)
            print("[notify] %s" % text, flush=True)
            threading.Thread(target=_notify_owner,
                             args=(text,), daemon=True).start()
            return self._send(200, {"ok": True})

        if path == "/api/result":
            cid = data.get("id")
            with _lock:
                _results.append({
                    "id": cid,
                    "cmd": data.get("cmd"),
                    "output": (data.get("output") or "")[:200000],
                    "exit": data.get("exit_code"),
                    "ts": time.time(),
                })
                del _results[:-MAX_RESULTS]
                _save_state()
            print("[result #%s] exit=%s (%d chars)" %
                  (cid, data.get("exit_code"), len(data.get("output") or "")), flush=True)
            return self._send(200, {"ok": True})

        return self._send(404, {"error": "unknown endpoint"})


def main():
    _load_state()

    # TLS-сервер — рабочий канал
    tls_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_ctx.load_cert_chain(str(CERT_FILE), str(KEY_FILE))
    tls = ThreadingHTTPServer(("0.0.0.0", TLS_PORT), Handler)
    tls.daemon_threads = True
    tls.socket = tls_ctx.wrap_socket(tls.socket, server_side=True)

    # Plain-сервер — только legacy-мост для первичного обновления старых клиентов
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True

    threading.Thread(target=tls.serve_forever, daemon=True).start()
    print("freyd-remote TLS on :%d, legacy-mesh on :%d (token: %s...)"
          % (TLS_PORT, PORT, TOKEN[:8]), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        with _lock:
            _save_state()
        print("stopped", flush=True)


if __name__ == "__main__":
    main()

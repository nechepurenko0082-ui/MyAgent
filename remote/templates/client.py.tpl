#!/usr/bin/env python3
"""Freyd remote client (Python, Windows/Linux).

Альтернатива client.ps1 — если на машине есть Python.
Запуск: python client.py
"""
import json
import ssl
import subprocess
import time
import urllib.error
import urllib.request

SERVER = "https://5.129.212.74:8443"
TOKEN = "__FREYD_TOKEN__"
POLL_WAIT = 25
CMDSHELL = True  # Windows: True (cmd), Linux: False (bash)

# TLS без CA: доверяем пиннингу на уровне клиента (см. client.ps1) либо
# первичной установке. Для критичного канала — добавить проверку отпечатка.
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE


def api(path, data=None, timeout=60):
    url = SERVER + path
    if data is None:
        req = urllib.request.Request(url, headers={"X-Token": TOKEN})
    else:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "X-Token": TOKEN, "Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
        return json.loads(r.read().decode("utf-8"))


def run_cmd(cmd):
    try:
        p = subprocess.run(
            cmd, shell=True, capture_output=True,
            timeout=600, executable="cmd" if CMDSHELL else None,
        )
        out = (p.stdout + p.stderr)
        for enc in ("utf-8", "cp866", "cp1251", "latin-1"):
            try:
                text = out.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = out.decode("utf-8", errors="replace")
        return text or "(пустой вывод)", p.returncode
    except subprocess.TimeoutExpired:
        return "(превышено время выполнения — 600 с)", -1
    except Exception as e:
        return f"(ошибка выполнения: {e})", -1


def main():
    print(f"Freyd client -> {SERVER}", flush=True)
    delay = 3
    while True:
        try:
            poll = api(f"/api/poll?wait={POLL_WAIT}", timeout=POLL_WAIT + 20)
            delay = 3
        except (urllib.error.URLError, OSError) as e:
            print(f"[!] нет связи: {e}; повтор через {delay} c", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue

        cid = poll.get("id")
        if not cid:
            continue

        cmd = poll.get("cmd") or ""
        print(f"-> {cmd}", flush=True)
        output, code = run_cmd(cmd)
        print(f"   exit={code}, {len(output)} символов", flush=True)

        try:
            api("/api/result", {"id": cid, "cmd": cmd,
                                "output": output[:200000], "exit_code": code})
        except (urllib.error.URLError, OSError) as e:
            print(f"[!] не удалось отправить результат: {e}", flush=True)


if __name__ == "__main__":
    main()

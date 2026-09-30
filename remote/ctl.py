#!/usr/bin/env python3
"""Управление удалённым ПК Саши с сервера.

  python3 ctl.py "команда"           — выполнить на ПК и дождаться вывода
  python3 ctl.py --status            — онлайн ли клиент, очередь, последние результаты
  python3 ctl.py --wait N "команда"  — ждать результат дольше (по умолчанию 60 с)
"""
import json
import ssl
import sys
import time
import urllib.request

SERVER = "https://127.0.0.1:8443"
TOKEN = open("/home/sasha/myagent/remote/token.txt").read().strip()

_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE  # localhost + пиннинг на стороне клиента


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


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    if args[0] == "--status":
        print(json.dumps(api("/api/status"), ensure_ascii=False, indent=2))
        return

    wait = 60
    if args[0] == "--wait":
        wait = int(args[1])
        args = args[2:]
    cmd = " ".join(args)
    if not cmd:
        print("пустая команда")
        sys.exit(1)

    got = api("/api/cmd", {"cmd": cmd})
    cid = got["id"]
    print(f"отправлено #{id} = {cid}" if False else f"отправлено #{cid}, жду...", file=sys.stderr)

    deadline = time.time() + wait
    while time.time() < deadline:
        res = api("/api/results?limit=50", timeout=30)
        for item in res.get("results", []):
            if item.get("id") == cid:
                print(f"--- exit={item.get('exit')} ---")
                print(item.get("output") or "")
                return
        time.sleep(2)
    print(f"!! за {wait} с ответа не пришло (клиент офлайн?)", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()

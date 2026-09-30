#!/usr/bin/env python3
"""Сборка клиентских файлов из шаблонов с реальными секретами.

Шаблоны (templates/*.tpl) лежат в git — без секретов.
Рабочие файлы (client.ps1 и т.д.) генерируются локально и в git не попадают.

Запуск: python3 remote/gen_clients.py
Источники секретов: remote/token.txt, remote/cert.pem.
"""
import hashlib
import ssl
import sys
from pathlib import Path

REMOTE_DIR = Path(__file__).resolve().parent
TEMPLATES = REMOTE_DIR / "templates"

PAIRS = [
    ("client.ps1.tpl", "client.ps1"),
    ("notify.ps1.tpl", "notify.ps1"),
    ("install.ps1.tpl", "install.ps1"),
    ("client.py.tpl", "client.py"),
]


def cert_fingerprint(pem_path):
    """SHA256-отпечаток сертификата в формате AB:CD:... (как openssl)."""
    der = ssl.PEM_cert_to_DER_cert(pem_path.read_text())
    return ":".join("%02X" % h for h in hashlib.sha256(der).digest())


def main():
    token_file = REMOTE_DIR / "token.txt"
    cert_file = REMOTE_DIR / "cert.pem"
    if not token_file.exists() or not cert_file.exists():
        print("!! нет token.txt или cert.pem", file=sys.stderr)
        sys.exit(1)

    token = token_file.read_text().strip()
    fp = cert_fingerprint(cert_file)

    for tpl_name, out_name in PAIRS:
        tpl = TEMPLATES / tpl_name
        if not tpl.exists():
            print("!! нет шаблона", tpl, file=sys.stderr)
            sys.exit(1)
        text = tpl.read_text()
        text = text.replace("__FREYD_TOKEN__", token)
        text = text.replace("__FREYD_CERT_FP__", fp)
        (REMOTE_DIR / out_name).write_text(text)
        print("сгенерирован", out_name)

    print("OK, отпечаток:", fp)


if __name__ == "__main__":
    main()

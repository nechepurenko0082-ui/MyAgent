# Freyd Remote — controlling Sasha's PC from the server (TLS)

Русский: [README.ru.md](README.ru.md)

## Architecture

```
Sasha/Freyd              server (server.py)                Sasha's PC (Windows)
     │  command to queue     │                                │
     │ ────────────────────► │                                │
     │                       │ ◄── client dials in ────────── │ (outgoing HTTPS)
     │                       │ ── delivers command ─────────► │
     │                       │ ◄── runs it, sends output ──── │
     │ ◄── ctl.py ────────── │                                │
```

## Channels

| Port | What | Purpose |
|---|---|---|
| **8443** | HTTPS (TLS 1.2, self-signed cert) | Working client channel. Encryption + certificate **pinning** |
| 8093 | plain HTTP | **Legacy bridge**: bootstrap for old clients only. Returns the self-update command, then can be closed |

The ISP only sees an encrypted stream between the two IPs (commands, output and
token stay hidden). TLS does not hide the fact that traffic flows between these
IPs or its volume — that would need a VPN/Tor.

## Encryption

- Certificate: `remote/cert.pem` + `remote/key.pem`, self-signed, 10 years.
  SAN: IP:5.129.212.74, IP:127.0.0.1, DNS:localhost.
- **Pinning**: clients (client.ps1, notify.ps1) accept ONLY the cert with our
  SHA-256 fingerprint (hardcoded as `$CertFp`). A substituted cert = connection
  refused.
- Show fingerprint: `openssl x509 -in remote/cert.pem -noout -fingerprint -sha256`
- Rotating the cert: generate a new one, put the new `$CertFp` into client.ps1
  and notify.ps1 — clients pick them up via self-update (see below).

## Client self-update

1. **Legacy**: an old client dials :8093 → the server immediately returns a
   bootstrap command (download new client.ps1/notify.ps1/update.ps1 from :8443).
   Integrity is checked by SHA256 inside update.ps1.
2. **TLS**: the new client checks its own file hashes against the server on every
   start (`GET /api/client_meta?h_client=..&h_notify=..`). Any mismatch → it
   downloads the files, verifies hashes and restarts itself via update.ps1.

So to roll out an update, just drop a new client.ps1 into `remote/` — all
clients pick it up at next start/reconnect.

## Files

| File | Purpose |
|---|---|
| `server.py` | HTTPS :8443 (working) + HTTP :8093 (legacy bridge) |
| `start_remote.sh` | Restart the server |
| `remote/client.ps1` | Windows client (TLS + pinning + self-update) |
| `remote/notify.ps1` | Shutdown/restart notifications (event 1074) |
| `remote/client.py` | Python variant of the client |
| `remote/install.ps1` | Installer: scheduled tasks FreydClient + FreydNotify |
| `remote/gen_clients.py` | Builds clients from `templates/*.tpl` with real secrets |
| `remote/ctl.py` | `python3 remote/ctl.py "command"` → output from the PC |
| `remote/token.txt` | Access token (X-Token, required everywhere) |
| `remote/cert.pem`, `remote/key.pem` | TLS certificate |

## Usage

```bash
python3 remote/ctl.py --status        # client online? queue?
python3 remote/ctl.py "ipconfig"      # run and wait for output
python3 remote/ctl.py --wait 120 "..."  # wait longer than 60 s
```

## Notes

- Port 443 needs root — that's why TLS lives on 8443. With extra rights (setcap
  or an iptables redirect) you can move to 443 via `FREYD_TLS_PORT`.
- Commands run through `cmd /c`, no hard timeout on the PowerShell client.
- The server autostarts on boot: cron `@reboot` → `start_remote.sh`.

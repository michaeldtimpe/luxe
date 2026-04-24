# zoleb

zoleb.com — privacy-awareness landing page. Shows visitors what identifying data their browser leaks (headers, Cloudflare edge info, Client Hints, navigator / screen / canvas fingerprints). Zero cookies, zero analytics.

## Stack

- Python 3, stdlib only — no pip, no requirements.txt, no deps
- Single-file `server.py` (~687 lines), listens on `127.0.0.1:8091`
- Fronted by nginx (TLS, rate-limit zone)
- systemd-managed via `zoleb.service` (hardened: `ProtectSystem=strict`, `PrivateTmp`, `NoNewPrivileges`)

## Entry points

- `server.py` — the whole app
- `install.sh` — idempotent installer (creates `/opt/zoleb`, `/var/log/zoleb`, installs unit + vhost)

## Dev loop

Production (server with nginx + systemd):
```bash
bash install.sh
sudo systemctl restart zoleb
curl http://127.0.0.1:8091/healthz
sudo tail -f /var/log/zoleb/visits.jsonl
```

Local testing: `python3 server.py` — that's it.

## Tests / lint

None configured.

## Agent gotchas

- **Stdlib-only, and it matters.** No requirements.txt, no pip install. `server.py` is the entire edit surface. Don't add libraries.
- **HTML/CSS/JS is inlined.** All frontend lives in `PAGE_TMPL` (a string literal in `server.py`). No separate asset files except the font.
- **Risk color assignments are split-brain.** `SERVER_FIELDS` tuple in Python + JS `add(label, value, source, note, risk)` calls together define fingerprint fields and risk levels (green/yellow/orange/red). Any field change needs both.
- **systemd hardening is strict.** Only `/var/log/zoleb` is writable. Never introduce code that writes elsewhere — it'll fail in production.
- **Log contains personal data.** `/var/log/zoleb/visits.jsonl` has IPs + browser fingerprints. Treat as sensitive. Do not exfiltrate off-box.
- **`style-guide/` is reference, not served.** Meant for adoption by other zoleb.com subdomains (never.zoleb.com, isomer.zoleb.com). Edit only when updating canonical palette tokens.

# isomer

Compliance audit platform (ISO 27001 + SOC 2) — browser-based, runs on isomer.zoleb.com behind nginx. Handles evidence upload, control tracking, and audit reports.

## Stack

- Python 3
- Flask + Flask-WTF + gunicorn
- SQLite persisted to `/data/` volume
- Docker; listens on `127.0.0.1:27001` (loopback only; nginx reverse-proxies)

## Entry points

- `app.py` — single-file app (~1138 lines): routes, DB, auth, CSRF, rate limit
- `entrypoint.py` — preflight creates `/data/uploads/`
- `deploy/isomer.zoleb.com.conf` — production nginx vhost (TLS + headers)

## Project layout

Flat. `data/` has control metadata (ISO 27001 93 Annex A controls, SOC 2 44 TSC criteria) as JSON. `templates/` has base layout + 8 views. Font `static/InterVariable.ttf` served locally.

## Dev loop

```bash
cp .env.example .env
python3 -c "import secrets; print('ISOMER_SECRET=' + secrets.token_urlsafe(48))" >> .env
docker compose up -d --build
docker logs isomer 2>&1 | grep -A1 "bootstrap admin"   # read one-time password
# → http://127.0.0.1:27001/
```

## Tests / lint

None configured.

## Agent gotchas

- **Loopback-only binding.** App binds `127.0.0.1:27001`. TLS and hardened headers live on nginx. Never expose Flask directly or bind `0.0.0.0`.
- **`ISOMER_SECRET` required.** App refuses to start without it. Also doubles as the CSRF key. Generate with `secrets.token_urlsafe(48)`.
- **Bootstrap password is one-time.** On an empty users table, a random admin password is printed to stderr once. Optional `ISOMER_BOOTSTRAP_PASSWORD` env on first boot presets it; ignored after.
- **Zip-slip defense.** Company imports validate every extracted path resolves inside `/data/uploads/<company_id>/`. Never bypass path normalization.
- **XSS defense on uploads.** SVG/HTML/XHTML/XML file uploads are served as `attachment`, not inline. Keep that `Content-Disposition` behavior.
- **Login rate limit.** In-memory 6 attempts / 5 min / `(username, client IP)`. Nginx applies a coarser per-IP limit on top. Don't remove either.

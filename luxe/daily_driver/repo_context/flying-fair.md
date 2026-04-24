# flying-fair

Flight price scanner: FastAPI + Uvicorn web UI plus a CLI for ad-hoc scans.
Compares airline prices across countries to find the best deal.

## Stack

- Python 3.9+ (no version pin)
- FastAPI + Uvicorn
- SQLite via `db.py` (auto-creates `/data/flyfair.db` on first run; schema in the `SCHEMA` constant)
- Dockerized; loopback-only, fronted by Cloudflare Access

## Entry points

- `app.py` — FastAPI server, REST `/api/scan`
- `flight_price_scanner.py` — standalone CLI (`python flight_price_scanner.py --origin JFK --destination LHR --date YYYY-MM-DD`)
- `start.sh` — launcher

## Dev loop

```bash
docker compose up -d                        # preferred
# or
pip install -r requirements.txt && uvicorn app:app
```

## Tests / lint

None configured.

## Agent gotchas

- **No auth in-process.** User identity is trusted from the `Cf-Access-Authenticated-User-Email` header injected by Cloudflare Access. Spoofable if the app is ever exposed without the proxy — don't add bypasses.
- **SerpAPI rate limit**: 6 requests/min per user. Rate-limit returns 429 with `Retry-After`. Don't retry in a tight loop.
- **Hardcoded currency fallbacks**: `flight_price_scanner.py:150–200` contains fallback exchange rates. Edits here silently shift price comparisons — touch only with care and testing.
- **Pydantic is strict**: IATA codes, ISO dates, bounded ints. Invalid input returns 422; don't loosen the validators.
- **Static mount**: `static/` is mounted at `/static/` in FastAPI — ensure referenced files exist before server start.
- **SQLite DB path**: `/data/flyfair.db` — Docker volume. Don't write files there outside the schema.

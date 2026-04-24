# never-say-yes

Kalshi NO-side prediction-market trading bot with three parallel strategy variants (baseline / optimized / reasoning) across weather, macro, and narrative contracts.

## Stack

- Python 3.12
- `aiohttp`, FastAPI, SQLAlchemy, psycopg2
- Postgres per variant
- Docker Compose orchestrates the lot

## Entry points

- `bot/main.py` — per-variant strategy loop
- `bot/market_fetcher.py` — shared market snapshot poller
- Env gates: `BOT_MODE={paper|live}`, `BOT_VARIANT={baseline|optimized|reasoning}`
- Dashboards at `:9093-9095` (`bot/dashboard.py`)

## Dev loop

```bash
cp .env.example .env          # fill POSTGRES_PASSWORD, KALSHI_API_KEY_ID
docker compose build && docker compose up -d
docker compose logs -f bot-baseline
curl http://127.0.0.1:9093/api/health
```

## Tests / lint

None configured.

## Agent gotchas

- **Kalshi API (March 2026 migration).** Use only fixed-point dollar-string fields (`yes_price_dollars`, `yes_bid_dollars`). Integer-cent legacy fields have been removed. Migrations that "normalize" to cents will break requests.
- **Status query mismatch.** `GET /markets?status=open` returns rows whose `status=active`. Passing `status=active` as a filter returns 400. The fetcher hits `open`; SQL filters permissively. Don't swap one for the other.
- **No market orders.** Kalshi removed `type=market` in Sept 2025. Emulated as immediate-or-cancel limits at the far-book side with a `buy_max_cost` cap. Don't reintroduce `type=market`.
- **Orderbook is bids-only.** No asks in the response. YES asks are synthesized as `1 - bid_no`. See `KalshiExchangeClient.get_order_book`.
- **Token ID format.** `"{ticker}:{yes|no}"` (e.g. `KXHIGHNY:no`). Split on the last `:`.
- **Paper collateral reserves.** `PaperExchangeClient` subtracts `price × size` on limit placement and refunds on cancel. Available = total − reserved. Exhausted cash → `insufficient_balance` rejection. Don't mutate the reserve separately.

# nothing-ever-happens

Polymarket trading bot (forked + patched) that trades a "nothing ever happens" strategy across three parallel profiles (baseline / optimized / reasoning).

## Stack

- Python 3.12 (`.python-version` pins)
- `asyncio`, `aiohttp`, `web3`
- Postgres (required for live mode)
- pytest (no config file)

## Entry points

- `bot/main.py` — async supervisor + strategy loop
- `scripts/` — `db_stats.py`, `export_db.py`, `wallet_history.py`
- Dashboard — `bot/dashboard.py` serves per-variant UIs at `127.0.0.1:9090+`

## Dev loop

```bash
cp config.example.json config.json
docker compose build && docker compose up -d
docker compose logs -f bot-baseline
```

Standalone: `pip install -r requirements.txt && python -m bot.main`.

## Tests / lint

- Tests: `python -m pytest -q` (tests in `tests/*.py` cover config, recovery, strategy, exchange clients, trade ledger).
- No ruff, mypy, black, or pre-commit configured.

## Agent gotchas

- **Triple safety gate for live trading.** All three must be true: `BOT_MODE=live`, `LIVE_TRADING_ENABLED=true`, `DRY_RUN=false`. Otherwise the bot transparently swaps in `PaperExchangeClient`. Do not simplify or bypass.
- **Async-only runtime.** Every I/O path is async. Blocking calls freeze the event loop — don't introduce `time.sleep` or sync HTTP clients.
- **Paper state persists.** `PaperExchangeClient` writes `/app/data/paper_state.json` on every fill; volumes are mounted per variant (`bot_data_{variant}`).
- **Strategy profile via env**. `STRATEGY_PROFILE` must match a key in `config.json`.
- **Postgres required for live**. `DATABASE_URL` is mandatory when `live_send_enabled=true`.
- **Live mode secrets**. `POLYGON_RPC_URL` and `PRIVATE_KEY` required for signing; signature type determines funder requirements. Don't commit.
- **Trade ledger is append-only**. Order events land in Postgres via `bot/trade_ledger.py`. Schema is implicit — don't DDL it manually.

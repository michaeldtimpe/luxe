# aurora

Autonomous L2 cryptocurrency trading system via Uniswap V3 — three parallel modes (dev / stage / prod) sharing code with live, paper, and validation paths. **Rust.**

## Stack

- Rust 1.85, workspace of 13 crates under `beta/`
- Redis shared data layer
- SQLite for the immutable trade ledger
- Systemd services for orchestration

GitHub tags this repo as Python, but that's the decommissioned Alpha directory. **Beta/Rust is the only live code.**

## Entry points

- `beta/crates/aurora-runner/src/main.rs` → binary `aurora` (mode selected via `AURORA_MODE` env var)
- Other binaries: `aurora-tui` (ratatui dashboard), `aurora-collector` (data pipeline), `aurora-harbor` (webhook receiver)
- Systemd units: `aurora-collector`, `aurora-dev`, `aurora-stage`, `aurora-prod` (under `scripts/` + `/etc/systemd/system/`)

## Dev loop

```bash
cd /opt/aurora/beta
~/.cargo/bin/cargo build --release
sudo systemctl restart aurora-dev aurora-stage aurora-prod
tail -5 /opt/aurora/logs/aurora-prod.log | python3 -m json.tool
docker exec aurora-redis redis-cli get portfolio:latest
# TUI:
REDIS_URL=redis://127.0.0.1:6379 target/release/aurora-tui
```

## Tests / lint

- `cargo test` (all crates; async tests via tokio)
- Alpha Python tests under `aurora/` remain but are reference only.

## Agent gotchas

- **Alpha is decommissioned.** Do not read or modify `aurora/` Python code, Alpha SQLite tables, or Alpha services. Beta is authoritative; git history covers legacy behavior.
- **Three modes, one binary.** dev / stage / prod differ only in CAL backend (Paper vs Live) and confidence model. A change to shared code affects all three unless mode-gated.
- **Secrets live in `.env`** (never committed). RPCs (`RPC_ARBITRUM`, `RPC_BASE`, …), wallet passphrases, API keys. `.env.example` shows the shape; treat real `.env` as sensitive.
- **Redis is shared across modes.** Race conditions on `portfolio:latest`, `divergence:*`, `bridge:active` are real. Per-instance keys are namespaced (`inventory:{label}:{chain}:{token}`); stick to the pattern.
- **Trade ledger is two-phase.** `beta_trades` gets a pending INSERT before tx, outcome UPDATE after. Orphaned pending rows indicate crashes — use them to diagnose, don't clean naively.
- **Circuit breaker in Redis (`sentinel:state`).** `is_halted=true` halts all trading (swap + bridge). Reset by clearing the key then restarting services — **order matters** per `LESSONS.md`.
- **Read `LESSONS.md` before edits.** 38 documented safety layers; each lesson encodes a past mistake + a Rule.
- **Async-only (tokio).** Blocking operations deadlock the runtime. Use `tokio::task::spawn_blocking` judiciously.
- **Bridge state TTLs.** `bridge:inflight:{id}` has a 2h TTL; `bridge:active` polled every 30s. Stale bridges block new ops if set logic fails.

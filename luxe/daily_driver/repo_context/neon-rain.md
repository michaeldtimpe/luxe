# neon-rain

Browser-based roguelike card game, cyberpunk hacker theme. Vanilla ES modules + Vite, zero runtime dependencies.

## Stack

- JavaScript (ES modules), Vite 6.x
- Zero runtime deps (keep it that way)
- Custom Node test harness (no Jest/Mocha)

## Entry points

- `src/main.js` — browser game UI
- `src/cli.js` — headless Node text mode (useful for testing game rules without the browser)

## Dev loop

```bash
npm install
npm run dev        # opens browser, hot-reload
npm run build      # dist/
npm run textmode   # headless CLI
npm test           # all 75 tests (~60s)
```

Other scripts: `test:basic` (12), `test:mechanics` (56), `test:vector`, `test:daemon`, `test:glitch` (full-game bot sims).

## Tests / lint

- Tests: `npm test` (uses `tests/test-helpers.js` for `createGame()`, assertions, finders)
- Lint / typing: none configured

## Agent gotchas

- **Card balance lives in JSON.** `src/data/cards/*.json` (7 files: jobs, programs, upgrades, ICE, events, data, cyberdecks, server recipes). Prefer editing JSON over code when changing game content.
- **Custom Node ESM loader.** `src/node-json-loader.js` handles `import ... from '...json'` for tests and CLI. Don't replace with default Node loader; tests will break.
- **Seeded RNG in tests.** `createGame({ rngSeed: ... })` uses deterministic shuffle/draw. Keep tests seeded for reproducibility.
- **EventBus is the nervous system.** All subsystem communication goes through pub/sub. Avoid direct references between systems.
- **Zero deps rule.** Don't add npm packages. Everything ships in the bundle.
- **Card art is referenced by cardId.** 75 PNGs in `assets/card_art/`. Adding a new card requires adding the asset file too or renders break.
- **Full-game bot tests are slow.** Vector/Daemon/Glitch each play to completion (~20-40 rounds). Expect ~60s total for `npm test`.

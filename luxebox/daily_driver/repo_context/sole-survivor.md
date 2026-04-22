# sole-survivor

Roguelike survival game aboard a damaged spacecraft — 20-minute oxygen timer, aliens, procedural ship generation. Browser game.

## Stack

- JavaScript (ES modules)
- Three.js r170
- Vite 6
- No runtime deps beyond Three.js

## Entry points

- `src/main.js` — initializes `Game` class on canvas
- `index.html` — served by Vite

## Project layout

`src/` split by domain: `player/`, `ship/`, `alien/`, `audio/`, `systems/`, `ui/`. `Game.js` is the orchestrator. Rendering is Three.js to canvas; helmet visor overlay is a DOM element.

## Dev loop

```bash
npm install
npm run dev       # hot reload
npm run build     # dist/
npm run preview   # preview the prod bundle
```

## Tests / lint

None configured.

## Agent gotchas

- **EventBus decoupling.** Systems talk via `src/EventBus.js` pub/sub. Changing an event name or payload ripples across multiple modules — search all subscribers before edits.
- **Procedural ship generation is seeded.** `src/utils.js` `createRNG`. Changing the seed mid-run breaks saved layouts.
- **Audio manager depends on external files.** Sounds expected in `assets/` but may not be in the repo. Missing files shouldn't crash, but visual cues may desync.
- **UI overlays are DOM, not WebGL.** Visor, menu, HUD are absolutely positioned divs. CSS changes in `style.css` directly affect game feel.
- **No tests yet.** Any change that touches game state is best validated manually in the browser. Consider adding a smoke test before landing larger refactors.

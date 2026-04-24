# elara

Local AI agent (Mixtral-8x7b via Ollama) with persistent session memory and multi-step task orchestration. ChromaDB backs the memory store.

## Stack

- Python 3
- Ollama (Mixtral-8x7b) served at `http://localhost:11434/v1`
- ChromaDB (`elara_memory` + `elara_tasks` collections)
- Embeddings via `nomic-embed-text`

## Entry points

- `elara_memory.py` — interactive chat
- `elara_task.py` — goal decomposition / task mode
- `tools/*.py` — auto-loaded tool functions (generated at runtime are persisted here)

## Dev loop

```bash
python3 elara_memory.py                                  # chat
python3 elara_task.py                                    # task mode
python3 baseline_check.py --output results/baseline.json # ~2 min health check
```

## Tests / lint

Seven standalone test suites (no unified runner):

- `baseline_check.py` (25 tests) — identity + basic behavior
- `task_orchestration_check.py` (36 tests)
- `memory_check.py` (37 tests)
- `json_output_check.py` (34 tests)
- `response_length_check.py` (25 tests)
- `multi_turn_check.py` (14 tests)
- `context_window_check.py` — context boundary

Each accepts `--output results/<name>.json` and optional `--category <name>`.

No lint / type-check / CI.

## Agent gotchas

- **Hardcoded ChromaDB path.** `elara_task.py`'s `CHROMA_PATH` points to `/Users/michaeltimpe/Library/CloudStorage/SynologyDrive-1200/1270/elara-mk.2/chroma_db`. Will fail on any other machine unless reassigned.
- **Ollama dependency.** Must be running on `:11434/v1`. Required: model alias `elara` (mixtral:8x7b) and embedding model `nomic-embed-text`, both pulled locally.
- **Context ceiling.** Practical limit ~16K tokens despite 32K config. Subtasks beyond 8–10 saturate and hallucinate. Chunk long work.
- **`keys.toml` secrets.** OpenChargeMap + TollGuru keys default blank and degrade gracefully. Keys are re-read on every tool call — no restart needed when rotating.
- **Tools are auto-persisted.** New functions written into `tools/*.py` at runtime are kept and reloaded on next start. Don't silently delete that directory.
- **Robust JSON parsing.** Four-stage fallback chain (direct → fence-strip → `{...}` extraction → `[...]` extraction) handles Mixtral's formatting variance. Don't simplify it.

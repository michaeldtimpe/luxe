"""Vendored BFCL multi_turn state-based evaluator (clean `base` subset).

PROVENANCE: copied from `bfcl_eval==2026.3.23` (the official Berkeley Function-Calling
Leaderboard eval). The multi_turn eval path is `tree_sitter`-free, so it runs in the luxe
runtime (`~/.venvs/MyEnv`) where `bfcl_eval` itself must NOT be installed (its
`tree_sitter==0.21.3` pin breaks `src/luxe/symbols.py`'s `tree_sitter_language_pack`).

Vendored VERBATIM except `bfcl_eval.* -> benchmarks.bfcl.multi_turn.*` import rewrites
(and `executable_backend_config.py`'s documented prefix rewrite + dropped excluded keys).
Do not "improve" this code — byte-faithfulness to upstream is what makes the parity gate
meaningful (luxe vendored verdict == official scorer verdict on identical decoded predictions).

Scope: the 8 deterministic stdlib/numpy involved classes used by `multi_turn_base`
(GorillaFileSystem, MathAPI, TradingBot, TwitterAPI/posting_api, TicketAPI, MessageAPI,
TravelAPI, VehicleControlAPI). Excluded (network / heavy-ML / non-deterministic):
WebSearchAPI, MemoryAPI_kv/_vector/_rec_sum, and the `long_context`/`miss_*`/`composite`
sub-categories. `long_context.py` is vendored verbatim (pure data, dep-free) but its
constants are only reachable under `self.long_context=True`, which `base` never sets.

Decoded-prediction contract (the grader input): `list[turn][step][call_string]`.
"""

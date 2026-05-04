"""luxe SWE-bench Verified adapter.

PRELIMINARY scaffolding (2026-05-03). Data model defined; dataset loader
and Docker harness wrapper deferred until decision point #1 in the plan
(`~/.claude/plans/fancy-honking-lerdorf.md`) — Docker on Apple Silicon
must be confirmed before pulling SWE-bench env images.

Schemas in `fixtures.py` reflect SWE-bench Verified row format from the
HuggingFace `princeton-nlp/SWE-bench_Verified` dataset (verified against
the official harness's `swebench/harness/utils.py` documentation
2024-12). Loader and harness modules will be added once the integration
path is approved.
"""

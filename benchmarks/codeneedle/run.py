"""CodeNeedle runner.

Usage:
  python -m benchmarks.codeneedle.run --output acceptance/codeneedle/<run_id>
  python -m benchmarks.codeneedle.run --corpus jquery.js --output ...
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from luxe.backend import Backend  # noqa: E402

from benchmarks._eval_common.dataset import sha256_file  # noqa: E402
from benchmarks._eval_common.meta import build_run_meta  # noqa: E402
from benchmarks.codeneedle.adapter import build_prompt  # noqa: E402
from benchmarks.codeneedle.grade import aggregate_items  # noqa: E402
from benchmarks.codeneedle.upstream.scorer import score as score_function  # noqa: E402

BENCHMARK_PROTOCOL_VERSION = "codeneedle/v1"
MANIFEST_PATH = REPO_ROOT / "benchmarks/codeneedle/manifest.json"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    out_root = Path(args.output)
    out_root.mkdir(parents=True, exist_ok=True)

    manifest = json.loads(MANIFEST_PATH.read_text())
    if manifest.get("protocol_version") != BENCHMARK_PROTOCOL_VERSION:
        print(
            f"WARNING: manifest protocol {manifest.get('protocol_version')!r} "
            f"!= benchmark {BENCHMARK_PROTOCOL_VERSION!r}",
            file=sys.stderr,
        )

    corpora = manifest["corpora"]
    if args.corpus:
        corpora = [c for c in corpora if c["corpus_name"] == args.corpus]
        if not corpora:
            print(f"corpus {args.corpus!r} not in manifest", file=sys.stderr)
            return 2

    backend = Backend(base_url=args.base_url, model=args.model)

    all_summaries: dict[str, dict] = {}
    for c in corpora:
        corpus_name = c["corpus_name"]
        out_dir = out_root / corpus_name.replace("/", "_")
        out_dir.mkdir(parents=True, exist_ok=True)

        corpus_path = REPO_ROOT / c["corpus_path"]
        corpus_text = corpus_path.read_text()
        actual_sha = sha256_file(corpus_path)
        if actual_sha != c["corpus_sha256"]:
            print(
                f"  {corpus_name}: ABORT — corpus sha256 drift "
                f"(manifest={c['corpus_sha256'][:16]}…, actual={actual_sha[:16]}…). "
                f"Rebuild manifest or restore fixture.",
                file=sys.stderr,
            )
            return 3

        # Context-fit warning
        est_tokens = len(corpus_text) // 3
        if est_tokens > args.num_ctx:
            print(
                f"  {corpus_name}: WARNING — estimated {est_tokens} tokens > "
                f"num_ctx={args.num_ctx}; results will be degraded",
                file=sys.stderr,
            )

        per_function_records: list[dict] = []
        for fn in c["functions"]:
            item_path = out_dir / f"{_safe_name(fn['name'])}.json"
            if args.resume and item_path.exists():
                per_function_records.append(json.loads(item_path.read_text()))
                continue

            prompt = build_prompt(
                file_contents=corpus_text,
                function_name=fn["name"],
                language=c["language"],
                n_lines=len(fn["primary_lines"]),
                source_path=corpus_path.name,
                multi_file=False,
                suppress_thinking=True,
            )

            t0 = time.time()
            try:
                resp = backend.chat(
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=args.max_tokens,
                    temperature=args.temperature,
                    num_ctx=args.num_ctx,
                )
                raw_output = resp.text or ""
                err = None
            except Exception as e:
                raw_output = ""
                err = f"{type(e).__name__}: {e}"
            wall_s = time.time() - t0

            fscore = score_function(
                name=fn["name"],
                primary=fn["primary_lines"],
                bonus=fn["bonus_lines"],
                predicted_text=raw_output,
            )
            record = asdict(fscore)
            record["error"] = err
            record["wall_s"] = wall_s
            record["raw_output"] = raw_output
            record["prompt_chars"] = len(prompt)
            item_path.write_text(json.dumps(record, indent=2, default=_json_default))
            per_function_records.append(record)
            print(
                f"  {corpus_name}/{fn['name']}: "
                f"primary={fscore.primary_matched}/{fscore.primary_total} "
                f"bonus={fscore.bonus_matched} halluc={fscore.hallucinated} "
                f"{'PASS' if fscore.passed else 'FAIL'} wall={wall_s:.1f}s"
            )

        summary_stats = aggregate_items(per_function_records)
        all_summaries[corpus_name] = summary_stats
        (out_dir / "summary.json").write_text(json.dumps(summary_stats, indent=2))

    sampling = {
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "num_ctx": args.num_ctx,
        "suppress_thinking": True,
    }
    meta = build_run_meta(
        benchmark_protocol_version=BENCHMARK_PROTOCOL_VERSION,
        model_id=args.model,
        sampling=sampling,
        backend_kind="http",
        context_window=args.num_ctx,
        backend_base_url=args.base_url,
        scoring={"method": "vendored_upstream_sequencematcher", "pass_threshold": 8},
        extra={"manifest_protocol": manifest.get("protocol_version")},
    )
    overall = {"meta": meta.to_dict(), "results_by_corpus": all_summaries}
    (out_root / "summary.json").write_text(json.dumps(overall, indent=2))

    for corpus_name, s in all_summaries.items():
        print(
            f"CodeNeedle {corpus_name} — {s['count']} fns, "
            f"pass_rate={s['pass_rate']:.2%}, "
            f"primary_match_rate={s['primary_match_rate']:.2%}, "
            f"hallucinations={s['hallucinated']}"
        )
    return 0


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "_-." else "_" for c in name)


def _json_default(obj):
    if hasattr(obj, "value"):
        return obj.value
    return str(obj)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m benchmarks.codeneedle.run")
    p.add_argument("--output", required=True, help="Output root directory.")
    p.add_argument("--corpus", default=None, help="Limit to one corpus by name (http_server.py | jquery.js).")
    p.add_argument("--model", default="Qwen3.6-35B-A3B-6bit")
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--max-tokens", type=int, default=2048)
    p.add_argument("--num-ctx", type=int, default=131072)
    p.add_argument("--resume", action="store_true", default=True)
    p.add_argument("--no-resume", dest="resume", action="store_false")
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())

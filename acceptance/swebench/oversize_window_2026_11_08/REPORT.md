# SWE-bench Verified n=75: oversize-read window lever, m5, 2026-11-08

**Verdict: not promoted. `LUXE_READ_OVERSIZE_WINDOW` stays opt-in (default OFF).**
The lever fires as designed and converts its target instance (django-11333, 0/3 to 3/3)
without the collateral loss the 128K arm took (django-10880 holds 3/3). But on the other
instances it touches it loses as much as it gains, and the 3-rep mean is 34.7 against
control's 35.7.

## What the lever does

Opt-in `LUXE_READ_OVERSIZE_WINDOW=1` (exact string; `src/luxe/tools/fs.py`,
`tools.sdd`). An unwindowed `read_file` of a file over `read_limit()` returns, as output
with no error, a header (size, line count, limit), an outline of the Python `def`/`class`
lines with line numbers (at most 25% of the budget), the first window of numbered lines
that fits, and the resume `offset=`. Without the flag the read is refused with the
existing message. Total output stays within `read_limit()`.

Motivation: in `acceptance/swebench/n75_ab_2026_11_06/REPORT.md`, control lost
django-11333 in all 3 reps because its step-0 whole read of `urls/resolvers.py` was
refused and it never opened `urls/base.py`. The question here was whether serving that
read at 32K recovers the 128K arm's win without its cost.

## Setup

- **Code:** branch `read-oversize-window`, 60bdc94 plus the lever (commit 5a0d6d9). Run
  from a separate worktree with its own venv.
- **Model / substrate:** champion `Qwen3.6-35B-A3B-6bit`, m5, oMLX 0.7.3, temp 0, config
  `configs/single_64gb_swebench.yaml` (32K, read budget 13,653 B), with
  `LUXE_READ_OVERSIZE_WINDOW=1`.
- **Control:** the 3 control reps of `n75_ab_2026_11_06` (2026-11-06, 65fd43c). Reused
  because no file under `src/` changed between 65fd43c and 60bdc94, the lever is
  byte-identical when OFF, and the oMLX build (0.7.3, installed 2026-11-02) and the weights
  snapshot are the same.
- **Probe first** (`probe_n5.json`, `probe/`, not graded): 5 instances with control
  refusals. Confirmed on the real bench path that oversize reads now return about 13.4 KB
  instead of 0 bytes, and that django-11333 now reads and patches `urls/base.py`.
- **Grading:** swebench 4.1.0 Docker harness, colima, 1800 s per instance. 0 harness errors
  in all 3 reps.

## Result

| Rep | control (11-06) | window | ctxonly 128K (11-06, reference) |
|---|---|---|---|
| rep 1 | 34 | 34 | 38 |
| rep 2 | 34 | 35 | 33 |
| rep 3 | 39 | 35 | 36 |
| **mean (sd)** | **35.7 (2.9)** | **34.7 (0.6)** | **35.7 (2.5)** |

n=71 (excluding the 4 ungradeable requests ids) gives the same numbers: none of them
resolved in any arm. Empty patches per rep: window 8 / 6 / 6, control 6 / 8 / 5.
Prediction wall: window 69 / 62 / 59 min, control 60 / 58 / 66.

**Firing:** window had 0 refused oversize reads and 18 / 18 / 20 served windows per rep.
Control had 11 / 10 / 13 refusals.

## Per instance

The lever can only change a trajectory at an oversize unwindowed read. So every instance
where neither arm hit one is outside its reach, and movement there is temp-0 noise.

**Instances the lever touched** (control refused at least once), resolve pattern across
reps 1/2/3 (E = empty patch):

| Instance | control | window | ctxonly | Reading |
|---|---|---|---|---|
| django-11333 | 000 | **111** | 111 | **Target converted.** The served window of `resolvers.py` is followed by a read of `urls/base.py` and a two-file patch, as in the 128K arm. |
| django-10880 | 111 | **111** | 000 | No oversize read in either 32K arm (the file is under the cap), so nothing changes. The 128K arm's loss came from reading more; this lever does not widen reads under the cap. |
| sphinx-10673 | 011 | 000 | 0E1 | **Loss.** With `toctree.py` served (2 windows in reps 1 and 3), the model patches the not-found branch of `TocTree.parse_content` instead of the url/self branch. Plausible, but `test_toctree_index` still fails. |
| pylint-6386 | 0E1 | 000 | 000 | Loss of control's one resolve. Control hits the 30-step limit in all 3 reps; window in 2 of 3. Weak attribution. |
| sphinx-10323 | 00E | E00 | 1EE | No change. |

Net on touched instances: +3 -2 -1 = 0.

**Untouched instances that moved** (no refusal in control, no served window in window):
sphinx-10449 (EE1 to 111) and matplotlib-13989 (101 to 111) up; django-10973 (111 to 011),
xarray-2905 (111 to 011), astropy-12907 (111 to 110), pytest-5262 (111 to 101) and
astropy-14096 (001 to 000) down; pylint-6528 (000 to EE0) and matplotlib-25775 (EE1 to
E0E). astropy-12907 and pytest-5262 have identical step counts in every run of both arms.
This is the near-tie noise documented for long trajectories at temp 0 (about ±5 at n=75).

## Conclusion

- Serving the oversize read does what the 11-06 analysis predicted on django-11333, and
  unlike the 128K window it does so without the django-10880 loss.
- It does not generalize. On the 4 other touched instances it is neutral or harmful, and
  the 3-rep mean is 1 below control (within noise, wrong direction). There is no case for
  turning it on by default.
- Kept as an opt-in: OFF is byte-identical, it is tested, and it is the right shape if
  chat sessions ever show refusal dead-ends. A chat-side arm would be needed first;
  this result is about the benchmark path only.
- Read with the 11-06 report: neither a bigger read budget nor serving the refused read
  moves the champion's SWE-bench score. The read path is not where the remaining misses
  are.

## Files

- `rep{1,2,3}/window/`: `predictions.json`, `summary.json`, `harness/harness_summary.json`
  (committed); per-instance JSON and harness logs stay local to m5.
- `probe_n5.json`, `probe/predictions.json`, `probe/summary.json`: the firing probe.
- `run_reps.sh` (launcher), `STATUS` (run log).
- `tabulate.txt` / `tabulate.json`: output of
  `acceptance/swebench/n75_ab_2026_11_06/tabulate.py` with
  `--arm control:<11-06 control reps> --arm window:<these reps> --arm ctxonly:<11-06 ctxonly reps>`.

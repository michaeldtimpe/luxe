"""`tokens_present` requirement kind (2026-11-20) and the document-config
fixture's canonical env-var set (scripts/doc_config_envcheck.py)."""

from __future__ import annotations

import importlib.util
from functools import partial
from pathlib import Path

import pytest

from luxe.spec import Requirement, Spec, spec_from_yaml_dict, spec_to_yaml_dict
from luxe.spec_validator import validate
from tests._git import git

_git = partial(git, strip=False)
ROOT = Path(__file__).resolve().parents[1]


def _req(tokens=("ALPHA", "BETA_X", "GAMMA"), frac=1.0, rid="R9") -> Requirement:
    return Requirement(id=rid, must="cover them", done_when="all appear",
                       kind="tokens_present", tokens=tuple(tokens), min_fraction=frac)


def _repo(tmp_path: Path, added: str) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("# base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "CONFIG.md").write_text(added)
    return repo, base


# --- schema -----------------------------------------------------------------

def test_requires_tokens_and_a_sane_fraction():
    with pytest.raises(ValueError, match="requires `tokens`"):
        _req(tokens=())
    for bad in (0.0, 1.5):
        with pytest.raises(ValueError, match="min_fraction"):
            _req(frac=bad)


def test_yaml_round_trip():
    d = {"goal": "g", "requirements": [{
        "id": "R1", "must": "m", "done_when": "d", "kind": "tokens_present",
        "tokens": ["A_B", "C_D"], "min_fraction": 0.5}]}
    spec = spec_from_yaml_dict(d)
    r = spec.requirements[0]
    assert r.tokens == ("A_B", "C_D") and r.min_fraction == 0.5
    assert spec_to_yaml_dict(spec)["requirements"][0]["tokens"] == ["A_B", "C_D"]


# --- evaluation -----------------------------------------------------------------

def test_counts_whole_words_only(tmp_path):
    # BETA_X inside BETA_X_LONG and ALPHA inside ALPHABET must not count.
    repo, base = _repo(tmp_path, "BETA_X_LONG ALPHABET\n`GAMMA` default 3\n")
    res = validate(Spec(goal="g", requirements=(_req(),)), repo, base).results[0]
    assert not res.satisfied
    assert "1/3 tokens covered" in res.detail
    assert "missing 2: ALPHA, BETA_X" in res.detail


def test_fraction_threshold(tmp_path):
    repo, base = _repo(tmp_path, "| ALPHA | x |\n| BETA_X | y |\n")
    spec = Spec(goal="g", requirements=(_req(frac=0.6), _req(frac=1.0, rid="R8")))
    a, b = validate(spec, repo, base).results
    assert a.satisfied and not b.satisfied
    assert "2/3" in a.detail and "need 1.8" in a.detail


def test_only_added_lines_count(tmp_path):
    repo, base = _repo(tmp_path, "nothing here\n")
    (repo / "README.md").write_text("# base\n")      # unchanged base text
    res = validate(Spec(goal="g", requirements=(_req(tokens=("BASE_ONLY",)),)),
                   repo, base).results[0]
    assert not res.satisfied and "0/1" in res.detail


def test_git_failure_is_not_evaluated_rather_than_unmet(tmp_path):
    repo, _ = _repo(tmp_path, "ALPHA\n")
    res = validate(Spec(goal="g", requirements=(_req(),)), repo, "0" * 40).results[0]
    assert not res.satisfied and "NOT EVALUATED" in res.detail


# --- the document-config fixture ---------------------------------------------------

def _checker():
    spec = importlib.util.spec_from_file_location(
        "doc_config_envcheck", ROOT / "scripts/doc_config_envcheck.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fixture_r4_loads_as_tokens_present():
    from benchmarks.maintain_suite.grade import Fixture
    m = _checker()
    fx = Fixture.from_dict(m._fixture())
    r4 = [r for r in fx.to_spec().requirements if r.id == "R4"][0]
    assert r4.kind == "tokens_present" and r4.min_fraction == 1.0
    assert len(r4.tokens) == 55 and "PM_NH_MARKET_REFRESH_INTERVAL_SEC" in r4.tokens
    # scripts/ vars are not the bot's
    assert "HEROKU_APP_NAME" not in r4.tokens and "POLYGONSCAN_API_KEY" not in r4.tokens


def test_env_reads_catches_every_read_shape():
    src = '''
import os
a = os.getenv("A_ONE")
b = os.environ.get("B_TWO", "x")
c = os.environ["C_THREE"]
d = _env_int(
    "D_FOUR",
    600,
)
def from_env():
    def _f(name, default): ...
    return _f("E_FIVE", 1.0)
e = os.environ.get(name)          # not a literal: ignored
f = settings.get("NOT_ENV")       # not an env reader: ignored
'''
    assert _checker().env_reads(src) == {"A_ONE", "B_TWO", "C_THREE", "D_FOUR", "E_FIVE"}


def test_fixture_r4_matches_derivation_when_cache_present():
    """Drift guard: R4 must equal what the checker derives from the fixture
    repo. Reads (never writes) the real fixture cache; conftest's hermetic
    HOME would hide it, so the real home dir comes from pwd. Skips where the
    cache is absent (CI)."""
    import os
    import pwd
    m = _checker()
    cache = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".luxe/fixture-cache/nothing-ever-happens"
    if not (cache / ".git").exists():
        pytest.skip("fixture cache not on this host")
    assert m.derive(cache, m._fixture()["base_sha"]) == sorted(m.r4_tokens())

"""Tests for src/luxe/modelstore.py — pulling model weights from a mounted
volume or from HuggingFace via the oMLX admin API. No network: the admin client
is driven through an httpx MockTransport.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from luxe import modelstore as ms


def _model_dir(path: Path, *, weight_bytes: int = 512) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.json").write_text(json.dumps({"model_type": "qwen2"}))
    (path / "model.safetensors").write_bytes(b"\0" * weight_bytes)
    return path


def _admin(handler, *, api_key: str = "k") -> ms.OmlxAdmin:
    a = ms.OmlxAdmin(base_url="http://127.0.0.1:8000", api_key=api_key)
    a._client = httpx.Client(base_url=a.base_url,
                             transport=httpx.MockTransport(handler))
    return a


# --- local store helpers ----------------------------------------------------


class TestStoreHelpers:
    def test_human_bytes(self):
        assert ms.human_bytes(512) == "512 B"
        assert ms.human_bytes(3 * 1024**3) == "3.0 GB"

    def test_store_name_for_strips_the_org(self):
        assert ms.store_name_for("mlx-community/Qwen3.6-27B-6bit") == "Qwen3.6-27B-6bit"
        assert ms.store_name_for("Qwen3.6-27B-6bit") == "Qwen3.6-27B-6bit"
        assert ms.store_name_for("/mnt/nas/Champ/") == "Champ"

    def test_is_model_dir_needs_config_and_weights(self, tmp_path):
        assert ms.is_model_dir(_model_dir(tmp_path / "good")) is True
        bare = tmp_path / "bare"
        bare.mkdir()
        assert ms.is_model_dir(bare) is False
        cfg_only = tmp_path / "cfg"
        cfg_only.mkdir()
        (cfg_only / "config.json").write_text("{}")
        assert ms.is_model_dir(cfg_only) is False
        assert ms.is_model_dir(tmp_path / "missing") is False

    def test_dir_size_sums_and_tolerates_unreadable(self, tmp_path):
        _model_dir(tmp_path / "m", weight_bytes=2048)
        assert ms.dir_size(tmp_path / "m") >= 2048

    def test_local_model_names(self, tmp_path):
        (tmp_path / "A").mkdir()
        (tmp_path / "B").mkdir()
        (tmp_path / "note.txt").write_text("x")
        assert ms.local_model_names(tmp_path) == ["A", "B"]
        assert ms.local_model_names(tmp_path / "nope") == []

    def test_model_state_distinguishes_ok_dangling_missing(self, tmp_path):
        """The 2026-07-30 finding: a store entry symlinked into a wiped HF
        cache LISTS as present but can never load. model_state names it."""
        _model_dir(tmp_path / "Real")
        (tmp_path / "Stub").symlink_to(tmp_path / "gone-snapshot")
        good_target = _model_dir(tmp_path / "elsewhere" / "snap")
        (tmp_path / "Linked").symlink_to(good_target)

        assert ms.model_state("Real", tmp_path) == "ok"
        assert ms.model_state("Linked", tmp_path) == "ok"
        assert ms.model_state("Stub", tmp_path) == "dangling"
        assert ms.model_state("Absent", tmp_path) == "missing"

    def test_remove_model_dir_and_symlink(self, tmp_path):
        _model_dir(tmp_path / "Real", weight_bytes=2048)
        target = _model_dir(tmp_path / "cache" / "snap")
        (tmp_path / "Linked").symlink_to(target)

        freed, note = ms.remove_model("Real", tmp_path)
        assert freed > 0 and not (tmp_path / "Real").exists()

        freed, note = ms.remove_model("Linked", tmp_path)
        assert freed == 0 and "target left alone" in note
        assert not (tmp_path / "Linked").is_symlink()
        assert (target / "config.json").is_file()   # cache copy untouched

        with pytest.raises(ms.ModelStoreError):
            ms.remove_model("Absent", tmp_path)


# --- mount discovery --------------------------------------------------------


class TestMountDiscovery:
    def test_matches_exact_and_hf_cache_forms(self):
        assert ms._matches("Qwen3.6-27B-6bit", "Qwen3.6-27B-6bit")
        assert ms._matches("models--mlx-community--Qwen3.6-27B-6bit", "Qwen3.6-27B-6bit")
        assert ms._matches("QWEN3.6-27B-6BIT", "Qwen3.6-27B-6bit")
        assert not ms._matches("Qwen3.6-27B-8bit", "Qwen3.6-27B-6bit")

    def test_finds_a_plain_export(self, tmp_path):
        _model_dir(tmp_path / "share" / "Champ")
        found = ms.scan_mounts_for("Champ", roots=[tmp_path])
        assert [s.kind for s in found] == ["mount"]
        assert found[0].ref.endswith("/Champ")
        assert found[0].size_bytes > 0

    def test_finds_an_hf_cache_layout_and_returns_the_snapshot(self, tmp_path):
        base = tmp_path / "hub" / "models--mlx-community--Champ"
        _model_dir(base / "snapshots" / "deadbeef")
        found = ms.scan_mounts_for("Champ", roots=[tmp_path])
        assert len(found) == 1
        assert found[0].ref.endswith("snapshots/deadbeef")

    def test_ignores_a_matching_dir_without_weights(self, tmp_path):
        (tmp_path / "Champ").mkdir()
        assert ms.scan_mounts_for("Champ", roots=[tmp_path]) == []

    def test_respects_the_depth_cap(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c" / "d" / "e"
        _model_dir(deep / "Champ")
        assert ms.scan_mounts_for("Champ", roots=[tmp_path], max_depth=2) == []
        assert ms.scan_mounts_for("Champ", roots=[tmp_path], max_depth=8)

    def test_respects_the_time_budget(self, tmp_path):
        _model_dir(tmp_path / "Champ")
        clock = iter([0.0, 100.0, 200.0, 300.0, 400.0])
        found = ms.scan_mounts_for("Champ", roots=[tmp_path], budget_s=1.0,
                                   now=lambda: next(clock))
        assert found == []          # budget blown before the first directory

    def test_unreadable_root_is_not_fatal(self, tmp_path, monkeypatch):
        """A dead SMB mount must not take down discovery (see fswalk)."""
        import os as _os

        real = _os.scandir

        def boom(path=".", *a, **k):
            if str(path).startswith(str(tmp_path)):
                raise OSError(60, "Operation timed out")
            return real(path, *a, **k)

        monkeypatch.setattr(_os, "scandir", boom)
        assert ms.scan_mounts_for("Champ", roots=[tmp_path]) == []

    def test_defaults_to_network_mounts(self, tmp_path, monkeypatch):
        _model_dir(tmp_path / "Champ")
        monkeypatch.setattr(ms, "network_mounts",
                            lambda: [(str(tmp_path), "smbfs", "//kappa/models")])
        found = ms.scan_mounts_for("Champ")
        assert len(found) == 1 and found[0].note == str(tmp_path)


# --- copy into the store ----------------------------------------------------


class TestCopyIntoStore:
    def _source(self, src: Path, name: str = "Champ") -> ms.ModelSource:
        return ms.ModelSource(kind="mount", ref=str(src), name=name,
                              size_bytes=ms.dir_size(src))

    def test_copies_and_reports(self, tmp_path):
        src = _model_dir(tmp_path / "nas" / "Champ", weight_bytes=4096)
        store = tmp_path / "store"
        seen: list[tuple[int, int]] = []

        res = ms.copy_into_store(self._source(src), models_dir=store,
                                 on_progress=lambda d, t: seen.append((d, t)))

        assert res.ok and Path(res.dest) == store / "Champ"
        assert (store / "Champ" / "model.safetensors").read_bytes() == b"\0" * 4096
        assert seen and seen[-1][0] > 0

    def test_dereferences_symlinks(self, tmp_path):
        """An HF-cache copy is symlinks into `blobs/` — they must be
        materialised, or the imported model is a pile of dangling links."""
        blobs = tmp_path / "blobs"
        blobs.mkdir()
        (blobs / "w").write_bytes(b"\1" * 128)
        src = tmp_path / "nas" / "Champ"
        src.mkdir(parents=True)
        (src / "config.json").write_text("{}")
        (src / "model.safetensors").symlink_to(blobs / "w")

        res = ms.copy_into_store(self._source(src), models_dir=tmp_path / "store")

        out = Path(res.dest) / "model.safetensors"
        assert not out.is_symlink()
        assert out.read_bytes() == b"\1" * 128

    def test_refuses_to_clobber_without_force(self, tmp_path):
        src = _model_dir(tmp_path / "nas" / "Champ")
        store = tmp_path / "store"
        (store / "Champ").mkdir(parents=True)
        with pytest.raises(ms.ModelStoreError, match="already exists"):
            ms.copy_into_store(self._source(src), models_dir=store)

    def test_force_replaces(self, tmp_path):
        src = _model_dir(tmp_path / "nas" / "Champ")
        store = tmp_path / "store"
        (store / "Champ").mkdir(parents=True)
        (store / "Champ" / "stale.txt").write_text("old")
        ms.copy_into_store(self._source(src), models_dir=store, force=True)
        assert not (store / "Champ" / "stale.txt").exists()
        assert (store / "Champ" / "config.json").exists()

    def test_rejects_a_non_model_source(self, tmp_path):
        junk = tmp_path / "junk"
        junk.mkdir()
        with pytest.raises(ms.ModelStoreError, match="not an MLX model"):
            ms.copy_into_store(ms.ModelSource(kind="mount", ref=str(junk),
                                              name="junk"),
                               models_dir=tmp_path / "store")

    def test_a_failed_copy_leaves_no_partial(self, tmp_path, monkeypatch):
        src = _model_dir(tmp_path / "nas" / "Champ")
        store = tmp_path / "store"
        monkeypatch.setattr(ms.shutil, "copy2",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
        with pytest.raises(OSError):
            ms.copy_into_store(self._source(src), models_dir=store)
        assert list(store.glob("*")) == []      # staging cleaned up
        assert not (store / "Champ").exists()   # never a half model

    def test_refuses_when_disk_is_full(self, tmp_path, monkeypatch):
        src = _model_dir(tmp_path / "nas" / "Champ")
        monkeypatch.setattr(ms.shutil, "disk_usage",
                            lambda p: type("U", (), {"free": 1024})())
        with pytest.raises(ms.ModelStoreError, match="free space"):
            ms.copy_into_store(self._source(src), models_dir=tmp_path / "store")


# --- oMLX admin client ------------------------------------------------------


class TestOmlxAdmin:
    def test_login_is_required_and_cached(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path == "/admin/api/login":
                return httpx.Response(200, json={"success": True})
            return httpx.Response(200, json={"tasks": []})

        a = _admin(handler)
        a.tasks()
        a.tasks()
        assert calls.count("/admin/api/login") == 1     # one login, then reused

    def test_login_failure_is_a_modelstore_error(self):
        a = _admin(lambda r: httpx.Response(401, json={"detail": "nope"}))
        with pytest.raises(ms.ModelStoreError, match="rejected the API key"):
            a.tasks()

    def test_missing_api_key_is_explained(self, monkeypatch):
        monkeypatch.delenv("OMLX_API_KEY", raising=False)
        a = _admin(lambda r: httpx.Response(200, json={}), api_key="")
        with pytest.raises(ms.ModelStoreError, match="no oMLX API key"):
            a.login()

    def test_unreachable_server_is_explained(self):
        def handler(request):
            raise httpx.ConnectError("refused")

        a = _admin(handler)
        with pytest.raises(ms.ModelStoreError, match="unreachable"):
            a.login()

    def test_search_parses_hits(self):
        def handler(request):
            if request.url.path == "/admin/api/login":
                return httpx.Response(200, json={"success": True})
            return httpx.Response(200, json={"models": [
                {"id": "mlx-community/Champ", "downloads": 12, "size": 4096,
                 "tags": ["mlx"]},
                {"nope": True},
            ]})

        hits = _admin(handler).search("champ")
        assert len(hits) == 1
        assert hits[0].repo_id == "mlx-community/Champ"
        assert hits[0].size_bytes == 4096

    def test_start_download_returns_the_task(self):
        def handler(request):
            if request.url.path == "/admin/api/login":
                return httpx.Response(200, json={"success": True})
            return httpx.Response(200, json={"success": True, "task": {
                "task_id": "t1", "repo_id": "org/Champ", "status": "pending",
                "progress": 0, "total_size": 100, "downloaded_size": 0}})

        task = _admin(handler).start_download("org/Champ")
        assert task.task_id == "t1" and task.status == "pending"
        assert task.done is False

    def test_start_download_without_a_task_errors(self):
        def handler(request):
            if request.url.path == "/admin/api/login":
                return httpx.Response(200, json={"success": True})
            return httpx.Response(200, json={"success": True})

        with pytest.raises(ms.ModelStoreError, match="did not return a download task"):
            _admin(handler).start_download("org/Champ")

    def test_http_error_on_download_surfaces_the_body(self):
        def handler(request):
            if request.url.path == "/admin/api/login":
                return httpx.Response(200, json={"success": True})
            return httpx.Response(400, text="unknown repo")

        with pytest.raises(ms.ModelStoreError, match="unknown repo"):
            _admin(handler).start_download("org/Nope")

    def test_wait_for_polls_until_done(self):
        states = iter([
            {"task_id": "t1", "repo_id": "r", "status": "downloading",
             "progress": 10, "total_size": 100, "downloaded_size": 10},
            {"task_id": "t1", "repo_id": "r", "status": "downloading",
             "progress": 60, "total_size": 100, "downloaded_size": 60},
            {"task_id": "t1", "repo_id": "r", "status": "completed",
             "progress": 100, "total_size": 100, "downloaded_size": 100},
        ])

        def handler(request):
            if request.url.path == "/admin/api/login":
                return httpx.Response(200, json={"success": True})
            return httpx.Response(200, json={"tasks": [next(states)]})

        seen: list[float] = []
        final = _admin(handler).wait_for(
            "t1", on_progress=lambda t: seen.append(t.progress),
            sleep=lambda s: None)
        assert final.status == "completed"
        assert seen == [10, 60, 100]

    def test_wait_for_stops_when_the_task_disappears(self):
        def handler(request):
            if request.url.path == "/admin/api/login":
                return httpx.Response(200, json={"success": True})
            return httpx.Response(200, json={"tasks": []})

        final = _admin(handler).wait_for("gone", sleep=lambda s: None)
        assert final.task_id == "gone"      # returns instead of looping forever


# --- source resolution ------------------------------------------------------


class TestResolveSources:
    def test_mounted_copy_outranks_huggingface(self, tmp_path, monkeypatch):
        _model_dir(tmp_path / "Champ")
        monkeypatch.setattr(ms, "network_mounts",
                            lambda: [(str(tmp_path), "smbfs", "//kappa/m")])
        srcs = ms.resolve_sources("mlx-community/Champ", admin=None)
        assert [s.kind for s in srcs] == ["mount", "hf"]

    def test_bare_name_with_no_mount_hit_has_nowhere_to_go(self, monkeypatch):
        monkeypatch.setattr(ms, "network_mounts", lambda: [])
        assert ms.resolve_sources("Champ") == []       # no org/ → not an HF ref

    def test_hf_only_when_mounts_are_skipped(self, monkeypatch):
        monkeypatch.setattr(ms, "network_mounts", lambda: [])
        srcs = ms.resolve_sources("org/Champ", include_mounts=False)
        assert [s.kind for s in srcs] == ["hf"]
        assert srcs[0].name == "Champ"


# --- Synology XSym stubs ----------------------------------------------------


def _xsym(path: Path, target: str) -> Path:
    """Write a Synology-format XSym stub (what an SMB share shows instead of a
    symlink): 1067 bytes, 4th line is the target."""
    body = b"XSym\n%04x\n%s\n%s\n" % (len(target), b"0" * 32, target.encode())
    path.write_bytes(body + b"\n" * (ms._XSYM_SIZE - len(body)))
    return path


class TestXsymStubs:
    """An HF-cache snapshot copied off a Synology share is all XSym stubs; a
    naive copy imports 1067-byte files instead of weights (the trap that broke
    a manual champion recovery — memory project_champion_weights_recovery_kappa).
    """

    def _snapshot(self, tmp_path: Path, *, weight_bytes: int = 8192) -> Path:
        blobs = tmp_path / "blobs"
        blobs.mkdir()
        (blobs / "sha-weights").write_bytes(b"\7" * weight_bytes)
        (blobs / "sha-config").write_text('{"model_type": "qwen2"}')
        snap = tmp_path / "snapshots" / "abc"
        snap.mkdir(parents=True)
        _xsym(snap / "model.safetensors", "../../blobs/sha-weights")
        _xsym(snap / "config.json", "../../blobs/sha-config")
        return snap

    def test_xsym_target_is_parsed(self, tmp_path):
        stub = _xsym(tmp_path / "f", "../../blobs/sha-weights")
        assert ms.xsym_target(stub) == "../../blobs/sha-weights"

    def test_plain_files_are_not_mistaken_for_stubs(self, tmp_path):
        p = tmp_path / "config.json"
        p.write_text("{}")
        assert ms.xsym_target(p) is None
        big = tmp_path / "pad"
        big.write_bytes(b"x" * ms._XSYM_SIZE)      # right size, wrong magic
        assert ms.xsym_target(big) is None

    def test_dir_size_measures_the_targets_not_the_stubs(self, tmp_path):
        snap = self._snapshot(tmp_path, weight_bytes=8192)
        assert ms.dir_size(snap) > 8192            # not 2 × 1067

    def test_copy_materialises_real_weights(self, tmp_path):
        snap = self._snapshot(tmp_path, weight_bytes=8192)
        src = ms.ModelSource(kind="mount", ref=str(snap), name="Champ",
                             size_bytes=ms.dir_size(snap))

        res = ms.copy_into_store(src, models_dir=tmp_path / "store")

        out = Path(res.dest) / "model.safetensors"
        assert out.read_bytes() == b"\7" * 8192    # real weights, not a stub
        assert out.stat().st_size == 8192
        assert (Path(res.dest) / "config.json").read_text().startswith("{")

    def test_dangling_stub_fails_loudly(self, tmp_path):
        snap = tmp_path / "snap"
        snap.mkdir()
        (snap / "config.json").write_text("{}")
        _xsym(snap / "model.safetensors", "../../blobs/missing")
        src = ms.ModelSource(kind="mount", ref=str(snap), name="Champ")

        with pytest.raises(ms.ModelStoreError, match="dangling Synology XSym"):
            ms.copy_into_store(src, models_dir=tmp_path / "store")
        assert not (tmp_path / "store" / "Champ").exists()

    def test_dangling_symlink_fails_loudly(self, tmp_path):
        """A model whose FIRST shard resolves but whose second is dangling
        passes the is_model_dir check — the copy must still refuse."""
        snap = _model_dir(tmp_path / "snap")          # config + shard 1 real
        (snap / "model-00002.safetensors").symlink_to(tmp_path / "gone")
        src = ms.ModelSource(kind="mount", ref=str(snap), name="Champ")

        with pytest.raises(ms.ModelStoreError, match="dangling symlink"):
            ms.copy_into_store(src, models_dir=tmp_path / "store")
        assert not (tmp_path / "store" / "Champ").exists()

    def test_nested_directories_are_preserved(self, tmp_path):
        src_dir = _model_dir(tmp_path / "m")
        (src_dir / "extra").mkdir()
        (src_dir / "extra" / "tokenizer.json").write_text("{}")
        src = ms.ModelSource(kind="mount", ref=str(src_dir), name="Champ",
                             size_bytes=ms.dir_size(src_dir))

        res = ms.copy_into_store(src, models_dir=tmp_path / "store")

        assert (Path(res.dest) / "extra" / "tokenizer.json").exists()

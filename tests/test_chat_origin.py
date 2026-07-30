"""Tests for chat/origin.py — is this model on local disk, on a network
volume, or on another machine entirely?

Motivated by 2026-07-29: nothing in the UI distinguished a session reading
weights off the local SSD from one streaming them over SMB, or from one whose
inference runs on a remote host over Tailscale.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from luxe.chat import origin
from luxe.chat import status as status_mod
from luxe.chat.session import ChatSession
from luxe.chat.slots import SlotManager
from luxe.chat.status import StatusState, fields
from luxe.config import PipelineConfig, RoleConfig

# Captured before the autouse fixture stubs it out, so the parser tests below
# can exercise the REAL implementation.
_REAL_NETWORK_MOUNTS = origin.network_mounts

LOCAL = "http://127.0.0.1:8000"
REMOTE = "http://m5.tailca7308.ts.net:8000"


class FakeBackend:
    def __init__(self, base_url=LOCAL, paths=None, models=None):
        self.base_url = base_url
        self._paths = paths or {}
        self._models = models if models is not None else list(self._paths)
        self.path_calls = 0
        self.list_calls = 0

    def model_paths(self):
        self.path_calls += 1
        return dict(self._paths)

    def list_models(self):
        self.list_calls += 1
        return list(self._models)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    origin.reset_cache()
    origin.reset_mount_cache()
    monkeypatch.setattr(origin, "network_mounts", lambda **k: [])
    yield
    origin.reset_cache()
    origin.reset_mount_cache()


# --- endpoint ---------------------------------------------------------------


@pytest.mark.parametrize("url", [LOCAL, "http://localhost:8000", "http://[::1]:8000"])
def test_localhost_endpoints_are_local(url):
    assert origin.endpoint_is_local(url) is True


def test_tailscale_endpoint_is_not_local():
    assert origin.endpoint_is_local(REMOTE) is False
    assert origin.endpoint_host(REMOTE) == "m5.tailca7308.ts.net"


# --- filesystem classification ----------------------------------------------


def test_local_path_is_local(tmp_path):
    org = origin.classify_path(tmp_path / "models" / "Champ")
    assert org.kind == "local"
    assert org.glyph == "⌂"
    assert org.is_over_the_network is False


def test_path_on_a_network_mount_is_network(tmp_path, monkeypatch):
    mount = tmp_path / "Volumes" / "models"
    monkeypatch.setattr(origin, "network_mounts",
                        lambda **k: [(str(mount), "smbfs", "//kappa/models")])
    org = origin.classify_path(mount / "Champ")
    assert org.kind == "network"
    assert org.glyph == "☁"
    assert "kappa" in org.detail and "smbfs" in org.detail
    assert org.is_over_the_network is True


def test_symlink_into_a_network_mount_is_network(tmp_path, monkeypatch):
    """`~/.omlx/models/<id>` is usually a symlink; the mount that matters is
    the TARGET's."""
    mount = tmp_path / "Volumes" / "models"
    real = mount / "Champ"
    real.mkdir(parents=True)
    link = tmp_path / "omlx" / "Champ"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    monkeypatch.setattr(origin, "network_mounts",
                        lambda **k: [(str(mount), "smbfs", "//kappa/models")])

    assert origin.classify_path(link).kind == "network"


def test_nested_mounts_pick_the_longest_match(tmp_path, monkeypatch):
    outer, inner = tmp_path / "Volumes", tmp_path / "Volumes" / "deep"
    monkeypatch.setattr(origin, "network_mounts", lambda **k: [
        (str(outer), "smbfs", "//a/outer"),
        (str(inner), "nfs", "//b/inner"),
    ])
    assert "inner" in origin.classify_path(inner / "Champ").detail


def test_cloud_sync_placeholder_tree_is_network(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    p = tmp_path / "Library" / "CloudStorage" / "SynologyDrive-1600" / "models" / "Champ"
    org = origin.classify_path(p)
    assert org.kind == "network"
    assert "SynologyDrive-1600" in org.detail


def test_missing_path_is_unknown():
    org = origin.classify_path("")
    assert org.kind == "unknown"
    assert org.glyph == "?"
    assert "unknown" in org.describe()


# --- endpoint-aware resolution ----------------------------------------------


def test_local_endpoint_classifies_each_model(tmp_path):
    b = FakeBackend(paths={"Champ": str(tmp_path / "Champ"), "Other": ""})
    orgs = origin.origins_for_backend(b)
    assert orgs["Champ"].kind == "local"
    assert orgs["Champ"].model_id == "Champ"
    assert orgs["Other"].kind == "unknown"


def test_remote_endpoint_marks_every_model_remote():
    b = FakeBackend(base_url=REMOTE, models=["Champ"])
    org = origin.origin_for(b, "Champ")
    assert org.kind == "remote"
    assert org.glyph == "⇅"
    assert org.detail == "m5.tailca7308.ts.net"
    assert "REMOTE" in org.describe()
    assert b.path_calls == 0          # never asks a remote host for local paths


def test_results_are_cached_per_endpoint(tmp_path):
    b = FakeBackend(paths={"Champ": str(tmp_path / "Champ")})
    origin.origin_for(b, "Champ")
    origin.origin_for(b, "Champ")
    assert b.path_calls == 1
    origin.origins_for_backend(b, force=True)
    assert b.path_calls == 2


def test_cached_origin_never_fetches(tmp_path):
    b = FakeBackend(paths={"Champ": str(tmp_path / "Champ")})
    assert origin.cached_origin_for(b, "Champ").kind == "unknown"
    assert b.path_calls == 0                       # cache-only: no HTTP
    origin.origins_for_backend(b)                  # prime (worker thread)
    assert origin.cached_origin_for(b, "Champ").kind == "local"
    assert b.path_calls == 1


def test_backend_errors_degrade_to_unknown():
    class Broken:
        base_url = LOCAL

        def model_paths(self):
            raise OSError(60, "Operation timed out")

    assert origin.origin_for(Broken(), "Champ").kind == "unknown"


def test_unreported_model_is_unknown_not_a_crash(tmp_path):
    b = FakeBackend(paths={"Champ": str(tmp_path / "Champ")})
    assert origin.origin_for(b, "SomethingElse").kind == "unknown"


# --- status bar -------------------------------------------------------------


@pytest.fixture
def slots(monkeypatch):
    from luxe.chat import slots as slots_module

    monkeypatch.setattr(slots_module, "Backend", lambda **k: FakeBackend())
    cfg = PipelineConfig(models={"monolith": "Champ"},
                         roles={"monolith": RoleConfig(model_key="monolith")})
    return SlotManager(cfg)


def _flat(segs) -> str:
    return " · ".join("".join(t for t, _p, _r in seg.spans) for seg in segs)


def test_status_bar_marks_local_weights(slots):
    out = _flat(fields(ChatSession(), slots, "",
                       StatusState(model="Champ", model_origin="local")))
    assert "⌂ Champ" in out


def test_status_bar_marks_network_weights(slots):
    out = _flat(fields(ChatSession(), slots, "",
                       StatusState(model="Champ", model_origin="network")))
    assert "☁ Champ" in out


def test_status_bar_marks_remote_endpoint(slots):
    out = _flat(fields(ChatSession(), slots, "",
                       StatusState(model="Champ", model_origin="remote")))
    assert "⇅ Champ" in out


def test_status_bar_adds_no_glyph_when_unknown(slots):
    out = _flat(fields(ChatSession(), slots, "", StatusState(model="Champ")))
    assert "Champ" in out
    for glyph in ("⌂", "☁", "⇅", "?"):
        assert glyph not in out


def test_network_origin_is_warn_coloured(slots):
    segs = fields(ChatSession(), slots, "",
                  StatusState(model="Champ", model_origin="network"))
    model_seg = segs[-1]
    glyph_span = model_seg.spans[0]
    assert glyph_span[0].startswith("☁")
    assert glyph_span[2] == status_mod.theme_mod.styles_for("warn")[1]


# --- startup notice ---------------------------------------------------------


def test_startup_notice_records_origin_and_names_the_location(slots, tmp_path):
    from luxe.chat import repl

    slots.backend = FakeBackend(paths={"Champ": str(tmp_path / "Champ")})
    state = StatusState()
    line = repl.model_origin_notice(slots, state)
    assert state.model_origin == "local"
    assert "Champ" in line and "local disk" in line


def test_startup_notice_flags_a_remote_endpoint(slots):
    from luxe.chat import repl

    slots.backend = FakeBackend(base_url=REMOTE, models=["Champ"])
    state = StatusState()
    line = repl.model_origin_notice(slots, state)
    assert state.model_origin == "remote"
    assert "REMOTE" in line and "m5.tailca7308.ts.net" in line
    assert "[yellow]" in line          # network crossings are called out


def test_startup_notice_survives_a_dead_endpoint(slots):
    from luxe.chat import repl

    class Broken:
        base_url = LOCAL

        def model_paths(self):
            raise OSError(60, "Operation timed out")

    slots.backend = Broken()
    state = StatusState()
    line = repl.model_origin_notice(slots, state)
    assert state.model_origin == "unknown"
    assert "Champ" in line


# --- mount(8) parsing -------------------------------------------------------


_MOUNT_SAMPLE = """/dev/disk3s5 on /System/Volumes/Data (apfs, local, journaled)
//timemachine@kappa._smb._tcp.local./timemachine on /Volumes/.timemachine/kappa (smbfs, nobrowse)
//mysterice@alpha._smb._tcp.local/archive on /Volumes/archive (smbfs, nodev, nosuid)
alpha:/volume1/media on /Volumes/media (nfs, nodev)
map auto_home on /System/Volumes/Data/home (autofs, automounted, nobrowse)
"""


def test_network_mounts_parses_mount_output(monkeypatch):
    import subprocess as sp

    class _Proc:
        stdout = _MOUNT_SAMPLE

    monkeypatch.setattr(sp, "run", lambda *a, **k: _Proc())
    origin.reset_mount_cache()

    mounts = _REAL_NETWORK_MOUNTS(force=True)
    points = {mp for mp, _fs, _src in mounts}
    assert points == {"/Volumes/.timemachine/kappa", "/Volumes/archive", "/Volumes/media"}
    assert ("/Volumes/media", "nfs", "alpha:/volume1/media") in mounts
    # apfs/autofs are local — never flagged
    assert not any("Data" in mp for mp in points)


def test_network_mounts_survives_a_broken_mount_command(monkeypatch):
    import subprocess as sp

    monkeypatch.setattr(sp, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    origin.reset_mount_cache()
    assert _REAL_NETWORK_MOUNTS(force=True) == []


def test_describe_trims_the_hf_snapshot_sha(tmp_path):
    p = ("/Users/x/.cache/huggingface/hub/models--mlx-community--Champ-6bit"
         "/snapshots/cb7e092ef8efe540bc3672c8929c4adbe5f4f759")
    org = origin.ModelOrigin(kind="local", detail=p)
    text = org.describe()
    assert "models--mlx-community--Champ-6bit" in text
    assert "cb7e092" not in text

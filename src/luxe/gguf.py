"""Read the chat template embedded in a GGUF file — header only, stdlib only.

neo serves GGUF weights through llama.cpp's `llama-server` with `jinja =
true`, so the template that runs on every prompt is the one stored in the
file's metadata (`tokenizer.chat_template`, plus any named
`tokenizer.chat_template.<name>` variants). `luxe.modelpins` hashes and lints
it exactly as it does an MLX directory's `chat_template.jinja`.

Format (GGUF v2/v3; v1 used 32-bit counts and predates every file the fleet
has, so it is refused rather than guessed at):

    magic "GGUF" · u32 version · u64 tensor_count · u64 kv_count
    kv_count × ( gguf_string key · u32 value_type · value )

A `gguf_string` is a u64 byte length plus UTF-8 bytes. Values are fixed-size
scalars, a string, or an ARRAY (u32 element type · u64 count · elements,
which may themselves be strings or arrays). Only string values whose key
starts with `tokenizer.chat_template` are decoded; everything else is
SKIPPED by seeking — the token vocabulary is a ~150k-entry string array, and
the tensors after the header are gigabytes, so nothing past the metadata is
ever read. Byte order is detected from the version field (a big-endian file
reads as a huge little-endian version).

Every malformed input — bad magic, truncation, an absurd count or length —
raises `GGUFError`; callers turn that into a diagnostic, never a traceback.
"""

from __future__ import annotations

import os
import struct
from typing import BinaryIO

_MAGIC = b"GGUF"
_TEMPLATE_PREFIX = "tokenizer.chat_template"

# value_type → struct format for fixed-size scalars.
_SCALAR = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?",
           10: "Q", 11: "q", 12: "d"}
_STRING, _ARRAY = 8, 9

# Sanity bounds: a real header has a few dozen KVs and a template is tens of
# KB. These only exist so a corrupt or hostile file cannot make the reader
# loop for hours or allocate gigabytes.
_MAX_KV = 1_000_000
_MAX_ARRAY = 1 << 32
_MAX_KEY = 1 << 16
_MAX_TEMPLATE = 16 << 20
_MAX_DEPTH = 8


class GGUFError(ValueError):
    """The file is not a readable GGUF v2/v3 header."""


class _Reader:
    def __init__(self, f: BinaryIO, size: int):
        self.f = f
        self.size = size
        self.e = "<"

    def _take(self, n: int) -> bytes:
        b = self.f.read(n)
        if len(b) != n:
            raise GGUFError("truncated GGUF header")
        return b

    def unpack(self, fmt: str):
        return struct.unpack(self.e + fmt, self._take(struct.calcsize(fmt)))[0]

    def skip(self, n: int) -> None:
        if n < 0 or self.f.tell() + n > self.size:
            raise GGUFError("truncated GGUF header")
        self.f.seek(n, os.SEEK_CUR)

    def string_len(self, cap: int | None = None) -> int:
        n = self.unpack("Q")
        if self.f.tell() + n > self.size or (cap is not None and n > cap):
            raise GGUFError(f"implausible GGUF string length {n}")
        return n

    def string(self, cap: int) -> str:
        return self._take(self.string_len(cap)).decode("utf-8", "replace")

    def skip_value(self, vtype: int, depth: int = 0) -> None:
        if vtype in _SCALAR:
            self.skip(struct.calcsize(_SCALAR[vtype]))
        elif vtype == _STRING:
            self.skip(self.string_len())
        elif vtype == _ARRAY:
            if depth >= _MAX_DEPTH:
                raise GGUFError("GGUF arrays nested too deep")
            etype = self.unpack("I")
            count = self.unpack("Q")
            if count > _MAX_ARRAY:
                raise GGUFError(f"implausible GGUF array length {count}")
            if etype in _SCALAR:
                self.skip(count * struct.calcsize(_SCALAR[etype]))
            else:
                for _ in range(count):
                    self.skip_value(etype, depth + 1)
        else:
            raise GGUFError(f"unknown GGUF value type {vtype}")


def read_chat_templates(path: str | os.PathLike[str]) -> dict[str, str]:
    """Every `tokenizer.chat_template*` string in the GGUF header at `path`,
    keyed by metadata key (empty dict when the file carries none).

    Raises `GGUFError` for anything that is not a readable v2/v3 header, and
    `OSError` when the file cannot be opened.
    """
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        r = _Reader(f, size)
        if r._take(4) != _MAGIC:
            raise GGUFError("not a GGUF file (bad magic)")
        raw = r._take(4)
        version = struct.unpack("<I", raw)[0]
        if version > 0xFFFF:                      # byte-swapped: big-endian
            r.e = ">"
            version = struct.unpack(">I", raw)[0]
        if version not in (2, 3):
            raise GGUFError(f"unsupported GGUF version {version} (need 2 or 3)")
        r.unpack("Q")                             # tensor_count — unused
        kv_count = r.unpack("Q")
        if kv_count > _MAX_KV:
            raise GGUFError(f"implausible GGUF metadata count {kv_count}")
        out: dict[str, str] = {}
        for _ in range(kv_count):
            key = r.string(_MAX_KEY)
            vtype = r.unpack("I")
            if vtype == _STRING and key.startswith(_TEMPLATE_PREFIX):
                out[key] = r.string(_MAX_TEMPLATE)
            else:
                r.skip_value(vtype)
        return out

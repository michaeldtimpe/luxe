#!/usr/bin/env python3
"""Recreate the five GGUF probe files used in REPORT.md's strict-flag check.

Usage: strict_probe.py <outdir>; then for each case run the patched pe_scan.py with
--no-default-paths [--strict] --path <outdir>/<case> and read the exit status.
"""
import os, struct, sys

d = sys.argv[1]

def hdr(kv):
    return b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0) + struct.pack("<Q", kv)

def kv_str(k, v):
    kb, vb = k.encode(), v.encode()
    return struct.pack("<Q", len(kb)) + kb + struct.pack("<I", 8) + struct.pack("<Q", len(vb)) + vb

cases = {
    "good": hdr(1) + kv_str("general.architecture", "llama"),
    "badmagic": b"XXXX" + hdr(0)[4:],
    "oldver": b"GGUF" + struct.pack("<I", 1) + b"\0" * 16,
    "shortheader": b"GGUF" + struct.pack("<I", 3) + b"\0\0",
    # second entry is a uint32 with only 2 bytes: _read_gguf_value raises
    "truncvalue": hdr(2) + kv_str("general.architecture", "llama")
    + struct.pack("<Q", 4) + b"ctxl" + struct.pack("<I", 4) + b"\x01\x00",
}
for name, blob in cases.items():
    os.makedirs(f"{d}/{name}", exist_ok=True)
    with open(f"{d}/{name}/m.gguf", "wb") as f:
        f.write(blob)

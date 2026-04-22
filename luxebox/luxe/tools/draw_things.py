"""Draw Things HTTP client — SD-webui-compatible /sdapi/v1/txt2img.

Draw Things (macOS app) exposes the stable-diffusion-webui-compatible HTTP
API when its HTTP server is enabled. We POST the usual request shape and
write the returned base64 PNG to disk.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

import httpx

from harness.backends import ToolDef

# Runtime-configured via set_endpoint(); defaults shown here.
_URL = "http://127.0.0.1:7860"
_OUTPUT_DIR = Path("~/luxe-images").expanduser()


def set_endpoint(url: str, output_dir: str | Path) -> None:
    """Override the Draw Things URL + output dir from LuxeConfig."""
    global _URL, _OUTPUT_DIR
    _URL = url.rstrip("/")
    _OUTPUT_DIR = Path(str(output_dir)).expanduser()


def _slug(text: str, limit: int = 50) -> str:
    t = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return t[:limit] or "image"


def tool_defs() -> list[ToolDef]:
    return [
        ToolDef(
            name="draw_things_generate",
            description=(
                "Generate an image via the local Draw Things app. Sends the "
                "prompt to Draw Things' HTTP API, saves the returned PNG to "
                "~/luxe-images/, and returns the saved path."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": (
                            "Positive prompt. Be specific and visual — "
                            "subject, setting, style, lighting, composition."
                        ),
                    },
                    "negative_prompt": {
                        "type": "string",
                        "description": "Things to exclude. Optional.",
                    },
                    "steps": {"type": "integer", "minimum": 1, "maximum": 40, "default": 8},
                    "width": {"type": "integer", "minimum": 256, "maximum": 1536, "default": 768},
                    "height": {"type": "integer", "minimum": 256, "maximum": 1536, "default": 768},
                },
                "required": ["prompt"],
            },
        )
    ]


def health_check(url: str | None = None, timeout: float = 2.0) -> tuple[bool, str]:
    target = (url or _URL).rstrip("/")
    try:
        r = httpx.get(f"{target}/sdapi/v1/options", timeout=timeout)
        return r.status_code == 200, f"HTTP {r.status_code}"
    except httpx.HTTPError as e:
        return False, f"{type(e).__name__}: {e}"


def generate(args: dict[str, Any]) -> tuple[Any, str | None]:
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return None, "empty prompt"

    payload = {
        "prompt": prompt,
        "negative_prompt": (args.get("negative_prompt") or "").strip(),
        "steps": int(args.get("steps") or 8),
        "width": int(args.get("width") or 768),
        "height": int(args.get("height") or 768),
    }

    try:
        with httpx.Client(timeout=600.0) as c:
            r = c.post(f"{_URL}/sdapi/v1/txt2img", json=payload)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        return None, f"draw_things request failed: {type(e).__name__}: {e}"
    except json.JSONDecodeError as e:
        return None, f"invalid JSON from draw_things: {e}"

    images = data.get("images") or []
    if not images:
        return None, "no images in response"

    png_bytes = base64.b64decode(images[0])
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    out = _OUTPUT_DIR / f"{ts}-{_slug(prompt)}.png"
    out.write_bytes(png_bytes)

    info = data.get("info") or ""
    seed: int | None = None
    if isinstance(info, str):
        try:
            seed = json.loads(info).get("seed")
        except json.JSONDecodeError:
            pass

    return (
        json.dumps(
            {
                "saved_to": str(out),
                "width": payload["width"],
                "height": payload["height"],
                "steps": payload["steps"],
                "seed": seed,
            }
        ),
        None,
    )


TOOL_FNS = {"draw_things_generate": generate}

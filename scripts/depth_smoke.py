#!/usr/bin/env python
"""Smoke-test the LOCAL depth provider (Apple Depth Pro) on one photo.

Runs the real model IN-PROCESS through the api.depth_provider seam — it never
touches the torch/depth_pro packages directly (those stay behind the seam) — and
prints the metric depth at the image center plus a few sanity stats. First run
downloads the weights (~1.9 GB) from Hugging Face; subsequent runs are fast.

    DEPTH_PROVIDER=local python scripts/depth_smoke.py path/to/photo.jpg

Exits non-zero (with a clear message) if the local stack isn't installed
(`pip install -r requirements-local.txt`) or the provider isn't 'local'.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Make `api` importable when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import depth_provider as dp  # noqa: E402
from api.photo import PhotoInput  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: DEPTH_PROVIDER=local python scripts/depth_smoke.py <photo>")
        return 2
    photo_path = Path(sys.argv[1])
    if not photo_path.exists():
        print(f"no such photo: {photo_path}")
        return 2

    provider = dp.get_provider_name()
    print(f"DEPTH_PROVIDER = {provider}")
    if provider != "local":
        print("set DEPTH_PROVIDER=local to exercise the in-process Depth Pro path.")
        return 2
    try:
        dp.check_provider_configured("local")
    except dp.DepthProviderError as e:
        print(f"local depth not ready: {e}")
        return 1

    mime = "image/png" if photo_path.suffix.lower() == ".png" else "image/jpeg"
    photo = PhotoInput(content=photo_path.read_bytes(), mime_type=mime)

    print("running Depth Pro (first run downloads weights ~1.9 GB)…")
    t0 = time.time()
    depth_map = dp.depth_map_mm(photo)  # HxW mm, or None
    dt = time.time() - t0
    if depth_map is None:
        print("depth unavailable (model failed) — see logs.")
        return 1

    import numpy as np

    h, w = depth_map.shape
    center_mm = dp.depth_mm_at(photo, 0.5, 0.5)
    finite = depth_map[np.isfinite(depth_map)]
    print(f"depth map: {w}x{h}  ({dt:.1f}s)")
    if center_mm is not None:
        print(f"center-pixel depth: {center_mm/1000.0:.3f} m  ({center_mm:.0f} mm)")
    if finite.size:
        print(
            f"scene depth range: {finite.min()/1000.0:.2f}–{finite.max()/1000.0:.2f} m "
            f"(median {np.median(finite)/1000.0:.2f} m)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

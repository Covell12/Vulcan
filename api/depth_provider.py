"""The ONE seam for all monocular metric-depth access in Vulcan.

Same shape as api/vision_provider.py: every other module calls
`estimate_scale(...)` and never imports a depth SDK or branches on a
provider's name — that lives entirely here. Which backend runs is chosen by
the DEPTH_PROVIDER env var; switching is one .env edit + a server restart.

Providers:
  - "none"      (default) — returns no estimates. The whole product MUST work
                 fully without depth: questions still get asked, critical dims
                 still require a user measurement, cross-check just reports
                 "unavailable". Depth is an optional convenience prior, never a
                 dependency.
  - "local"     — Apple's open-source Depth Pro running IN-PROCESS (metric depth
                 + estimated focal length, no network at inference). Needs the
                 optional heavy deps in requirements-local.txt (torch + the
                 depth_pro package); the model loads lazily on first use and its
                 weights auto-download from Hugging Face (apple/DepthPro) once,
                 cached under ~/.cache/huggingface.
  - "replicate" — the same class of model via REPLICATE_API_TOKEN (a hosted cog
                 that must return per-pixel metric depth + focal length).

Both metric providers satisfy the SAME contract (`_run_depth_model` →
(depth_map_meters, fx, fy)); the pure metric geometry downstream
(`_region_size_mm`, occlusion) is identical regardless of which supplies the
depth, and is unit-tested against synthetic depth maps.

DESIGN NOTE (replicate caveat, verified July 2026): no public Replicate wrapper
returns raw metric depth — the popular `garg-aayush/ml-depth-pro` computes metric
depth + focal internally but *discards* them, returning only a colorized
visualization PNG. So the replicate path requires a cog that returns the metric
shape and raises a clear DepthProviderError on a plain visualization image rather
than inventing numbers. `local` sidesteps this entirely by running Depth Pro
here and reading its raw metric output directly — which is why it's the
recommended way to actually turn photos into sizes/occlusion.

ISOLATION: torch and depth_pro are imported ONLY inside this module and ONLY
lazily (when local depth actually runs), so the base install stays light and the
seam holds — enforced by the grep test in tests/test_depth_provider.py.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv

from api.photo import PhotoInput

logger = logging.getLogger("vulcan.depth")

# override=True: .env is authoritative over shell/OS env — see the note in
# api/vision_provider.py. Safe for tests (monkeypatch runs post-import) and
# deployments (no .env -> no-op).
load_dotenv(override=True)

_DEFAULT_MODEL = "garg-aayush/ml-depth-pro"

# Assumed horizontal field of view (degrees) used to derive a focal length
# ONLY when the depth model doesn't return one. A rough phone-camera default;
# documented so nobody mistakes the resulting sizes for calibrated truth.
_ASSUMED_HFOV_DEG = 60.0

# Depth-prior confidence is deliberately capped low: an uncalibrated
# single-image metric-depth estimate is a suggestion, not a measurement.
_BASE_CONFIDENCE = 0.45
_MAX_CONFIDENCE = 0.5


class DepthProviderError(RuntimeError):
    """The only exception type that may escape this module. Every raw SDK /
    network / decode failure is caught and re-raised as one of these with a
    human-readable cause."""


@dataclass(frozen=True)
class ScaleRegion:
    """A place on a photo we want a metric size for, taken from a question's
    overlay. `points` are normalized [x, y] in [0, 1]. An arrow/line with two
    endpoints measures a length; a circle needs >= 2 points (center + edge, or
    a bounding span) to have a measurable size."""

    dim_name: str
    shape: str  # "arrow" | "line" | "circle"
    points: list[list[float]]
    photo_index: int = 0


@dataclass(frozen=True)
class ScaleEstimate:
    dim_name: str
    value_mm: float
    confidence: float


def _env_value_set(name: str) -> bool:
    """True only if an env var holds a real value. Guards the python-dotenv
    inline-comment landmine (`KEY=   # comment` loads '# comment' as the value),
    so a leading '#' after stripping counts as unset."""
    value = os.getenv(name, "").strip()
    return bool(value) and not value.startswith("#")


def get_provider_name() -> str:
    return os.getenv("DEPTH_PROVIDER", "none").strip().lower()


def get_model_name() -> str:
    return os.getenv("DEPTH_MODEL") or _DEFAULT_MODEL


_SUPPORTED_PROVIDERS = ("none", "local", "replicate")


def check_provider_configured(provider: str | None = None) -> None:
    """Fail fast (at server startup) when the selected provider isn't ready: a
    missing REPLICATE_API_TOKEN for 'replicate', or the un-installed local depth
    stack for 'local'. The default provider ("none") never needs anything."""
    provider = provider or get_provider_name()
    if provider == "none":
        return
    if provider == "local":
        ok, reason = _local_available()
        if not ok:
            raise DepthProviderError(reason)
        return
    if provider == "replicate":
        if not _env_value_set("REPLICATE_API_TOKEN"):
            raise DepthProviderError(
                "DEPTH_PROVIDER is 'replicate' but REPLICATE_API_TOKEN is not set. "
                "Add REPLICATE_API_TOKEN=... to .env (see .env.example), or set "
                "DEPTH_PROVIDER=none to run without a depth prior."
            )
        return
    raise DepthProviderError(
        f"Unknown DEPTH_PROVIDER '{provider}'. Supported: {list(_SUPPORTED_PROVIDERS)}"
    )


def estimate_scale(
    photo: PhotoInput, regions: list[ScaleRegion]
) -> list[ScaleEstimate]:
    """Estimate the metric size (mm) of each region on `photo`. Returns an
    estimate only for regions the depth model can actually size — the list may
    be shorter than `regions`, or empty. All regions are assumed to be on the
    given photo (the caller groups by photo_index). Never raises for the "none"
    provider; for "replicate" any failure surfaces as DepthProviderError."""
    provider = get_provider_name()
    if provider == "none":
        return []
    if provider in ("local", "replicate"):
        if not regions:
            return []
        return _estimate_metric(photo, regions)
    raise DepthProviderError(
        f"Unknown DEPTH_PROVIDER '{provider}'. Supported: {list(_SUPPORTED_PROVIDERS)}"
    )


def depth_mm_at(photo: PhotoInput, x_norm: float, y_norm: float) -> float | None:
    """Best-effort metric depth (mm) at a normalized image point [x, y] in
    [0, 1]. Used by the ghost composite (api/composite.py) to place the part at
    the true distance of the surface the user circled.

    Returns None whenever depth is unavailable — DEPTH_PROVIDER=none, or the
    model failed/couldn't size that pixel — so the composite falls back to a
    non-depth scale. Unlike the rest of this module it NEVER raises: depth here
    is a pure convenience, and a preview must not be able to break because a
    depth backend hiccuped."""
    if get_provider_name() not in ("local", "replicate"):
        return None
    try:
        depth_map, _fx, _fy = _run_depth_model(photo)
        h, w = depth_map.shape
        d = _sample_depth(depth_map, x_norm * (w - 1), y_norm * (h - 1))
        if not math.isfinite(d) or d <= 0:
            return None
        return d * 1000.0
    except Exception:
        return None


def depth_map_mm(photo: PhotoInput):
    """Best-effort FULL-SCENE metric depth map (HxW, millimetres) for the whole
    photo — used by the ghost composite (api/composite.py) to occlude the part
    behind nearer foreground objects. Invalid/background pixels come back as +inf
    (they never occlude). Returns None whenever a depth map is unavailable
    (DEPTH_PROVIDER=none or any model failure); like depth_mm_at it NEVER raises,
    because occlusion is a pure preview nicety that must never break the join."""
    if get_provider_name() not in ("local", "replicate"):
        return None
    try:
        import numpy as np

        depth_map, _fx, _fy = _run_depth_model(photo)
        arr = np.asarray(depth_map, dtype=float)
        if arr.ndim != 2 or arr.size == 0:
            return None
        valid = np.isfinite(arr) & (arr > 0)
        return np.where(valid, arr * 1000.0, np.inf)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Pure metric geometry (no network, unit-tested against synthetic depth maps)
# ---------------------------------------------------------------------------


def _sample_depth(depth_map: Any, u: float, v: float, window: int = 2) -> float:
    """Median of the valid (finite, positive) depth samples in a small window
    around pixel (u, v). Returns NaN if nothing valid is nearby."""
    import numpy as np

    h, w = depth_map.shape
    ui, vi = int(round(u)), int(round(v))
    # A point outside the frame has no depth — return NaN rather than let a
    # negative slice-stop (min(w, ui+window+1) < 0) wrap around and sample an
    # unrelated region. Reachable from an out-of-[0,1] annotation point.
    if not (0 <= ui < w and 0 <= vi < h):
        return float("nan")
    u0, u1 = max(0, ui - window), min(w, ui + window + 1)
    v0, v1 = max(0, vi - window), min(h, vi + window + 1)
    patch = depth_map[v0:v1, u0:u1]
    valid = patch[np.isfinite(patch) & (patch > 0)]
    if valid.size == 0:
        return float("nan")
    return float(np.median(valid))


def _backproject(
    u: float, v: float, depth: float, fx: float, fy: float, cx: float, cy: float
):
    """Pinhole back-projection of a pixel + its metric depth to a 3D camera-
    space point (meters)."""
    x = (u - cx) * depth / fx
    y = (v - cy) * depth / fy
    return (x, y, depth)


def _region_size_mm(
    depth_map: Any, fx: float, fy: float, region: ScaleRegion
) -> float | None:
    """Metric size (mm) of a region, or None if it can't be measured (too few
    points, or depth invalid at the endpoints). Pure function of its inputs."""
    import numpy as np

    points = region.points or []
    if len(points) < 2:
        return None  # a single point has no measurable size

    h, w = depth_map.shape
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0

    # Use the two extreme points (first and last) as the span endpoints.
    (x0, y0), (x1, y1) = points[0], points[-1]
    u0, v0 = x0 * (w - 1), y0 * (h - 1)
    u1, v1 = x1 * (w - 1), y1 * (h - 1)

    d0 = _sample_depth(depth_map, u0, v0)
    d1 = _sample_depth(depth_map, u1, v1)
    if not (math.isfinite(d0) and math.isfinite(d1)):
        return None

    p0 = np.array(_backproject(u0, v0, d0, fx, fy, cx, cy))
    p1 = np.array(_backproject(u1, v1, d1, fx, fy, cx, cy))
    size_m = float(np.linalg.norm(p0 - p1))
    if not math.isfinite(size_m) or size_m <= 0:
        return None
    return size_m * 1000.0


def _region_confidence(depth_map: Any, region: ScaleRegion) -> float:
    """A deliberately modest confidence: high only if depth is valid at both
    endpoints, and never above _MAX_CONFIDENCE."""
    points = region.points or []
    if len(points) < 2:
        return 0.0
    h, w = depth_map.shape
    valid = 0
    for x, y in (points[0], points[-1]):
        d = _sample_depth(depth_map, x * (w - 1), y * (h - 1))
        if math.isfinite(d):
            valid += 1
    return min(_MAX_CONFIDENCE, _BASE_CONFIDENCE * (valid / 2.0))


# ---------------------------------------------------------------------------
# Metric-depth dispatch (local OR replicate) + the two adapters. torch/depth_pro
# and replicate are imported ONLY here, lazily.
# ---------------------------------------------------------------------------


def _run_depth_model(photo: PhotoInput):
    """Dispatch to the configured metric-depth backend and return the SAME shape
    for both: (depth_map, fx, fy) where depth_map is an HxW float array of METERS
    and fx/fy are focal lengths in pixels. Raises DepthProviderError on failure."""
    provider = get_provider_name()
    if provider == "local":
        return _run_local_model(photo)
    return _run_replicate_model(photo)


def _estimate_metric(
    photo: PhotoInput, regions: list[ScaleRegion]
) -> list[ScaleEstimate]:
    # Wrap the WHOLE path so the module's contract holds: only DepthProviderError
    # may escape. This also catches an ImportError from a missing/broken backend
    # dependency (`replicate`/`torch`/`depth_pro`/`numpy`/`pillow`/`httpx` — their
    # imports are deferred into the functions below), which would otherwise leak
    # raw and break the graceful-degradation path in api/intents.py.
    try:
        depth_map, fx, fy = _run_depth_model(photo)
        estimates: list[ScaleEstimate] = []
        for region in regions:
            size_mm = _region_size_mm(depth_map, fx, fy, region)
            if size_mm is None:
                continue
            estimates.append(
                ScaleEstimate(
                    dim_name=region.dim_name,
                    value_mm=round(size_mm, 1),
                    confidence=round(_region_confidence(depth_map, region), 3),
                )
            )
        return estimates
    except DepthProviderError:
        raise
    except Exception as e:
        raise DepthProviderError(
            f"depth estimation failed ({type(e).__name__}: {e})"
        ) from e


# ---------------------------------------------------------------------------
# Local adapter — Apple Depth Pro, IN-PROCESS. torch + depth_pro are imported
# ONLY here and ONLY lazily, so the base install stays light and the seam holds.
# ---------------------------------------------------------------------------

# Loaded once per process and cached (weights are ~1.9 GB; re-loading per request
# would be absurd). A tuple (model, transform, device) or None.
_LOCAL_MODEL: Any = None


def _local_available() -> tuple[bool, str]:
    """Is the local Depth Pro stack importable? Returns (ok, reason). Uses
    importlib so we don't actually import (and pay for) torch just to check."""
    import importlib.util

    for pkg in ("torch", "depth_pro"):
        if importlib.util.find_spec(pkg) is None:
            return False, (
                f"DEPTH_PROVIDER is 'local' but the '{pkg}' package isn't installed. "
                "Install the local depth stack with "
                "`pip install -r requirements-local.txt`, or set DEPTH_PROVIDER=none."
            )
    return True, ""


def _local_device(torch):
    """Best available torch device, overridable with DEPTH_LOCAL_DEVICE
    (auto|cpu|mps|cuda). Apple Silicon MPS > CUDA > CPU."""
    want = os.getenv("DEPTH_LOCAL_DEVICE", "auto").strip().lower()
    if want in ("cpu", "mps", "cuda"):
        return torch.device(want)
    if torch.cuda.is_available():
        return torch.device("cuda")
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def _load_local_model():
    """Lazily load Depth Pro once, caching it for the process. Auto-downloads the
    weights from Hugging Face (apple/DepthPro) on first use if they're not already
    on disk, with a clear log line so a first-run pause is explained."""
    global _LOCAL_MODEL
    if _LOCAL_MODEL is not None:
        return _LOCAL_MODEL

    import dataclasses

    import depth_pro
    import torch
    from depth_pro.depth_pro import DEFAULT_MONODEPTH_CONFIG_DICT

    device = _local_device(torch)
    ckpt = getattr(DEFAULT_MONODEPTH_CONFIG_DICT, "checkpoint_uri", None)
    if not (ckpt and os.path.exists(ckpt)):
        from huggingface_hub import hf_hub_download

        logger.info(
            "Depth Pro (local): downloading weights apple/DepthPro (~1.9 GB) on "
            "first use; cached under ~/.cache/huggingface…"
        )
        ckpt = hf_hub_download("apple/DepthPro", "depth_pro.pt")
    config = dataclasses.replace(DEFAULT_MONODEPTH_CONFIG_DICT, checkpoint_uri=ckpt)
    logger.info("Depth Pro (local): loading model on %s…", device)
    model, transform = depth_pro.create_model_and_transforms(
        config=config, device=device, precision=torch.float32
    )
    model.eval()
    _LOCAL_MODEL = (model, transform, device)
    return _LOCAL_MODEL


def _run_local_model(photo: PhotoInput):
    """Run Apple Depth Pro in-process on the photo → (depth_map_meters HxW, fx, fy).
    Focal length is estimated by the model itself (the uncalibrated path)."""
    import io

    import numpy as np
    import torch
    from PIL import Image, ImageOps

    model, transform, _device = _load_local_model()
    img = Image.open(io.BytesIO(photo.content))
    img = ImageOps.exif_transpose(img).convert("RGB")  # match the composite frame
    # np.array (a writable copy, not np.asarray's read-only view) — torchvision's
    # ToTensor warns on a non-writable array.
    tensor = transform(np.array(img))
    with torch.no_grad():
        pred = model.infer(tensor, f_px=None)
    depth = np.asarray(pred["depth"].detach().to("cpu"), dtype=float)  # meters, HxW
    fpx = pred.get("focallength_px")
    fx = float(fpx) if fpx is not None else _focal_from_fov(img.width)
    return depth, fx, fx  # square pixels (fy == fx)


def _run_replicate_model(photo: PhotoInput):
    """Call the configured Replicate model and return (depth_map, fx, fy) where
    depth_map is an HxW float array of METERS and fx/fy are focal lengths in
    pixels. Encapsulates ALL model-specific decoding.

    Contract the model must satisfy: return metric per-pixel depth and (ideally)
    a focal length. Accepted output shapes:
      - a dict with a "depth" file (npz/npy/tiff/exr of meters) and optionally
        "focallength_px";
      - a single numeric depth file (npz/npy/tiff) — focal length is then
        derived from an assumed field of view.
    A plain visualization image (PNG/JPEG) is rejected with a clear error,
    because it carries no absolute scale.
    """
    import base64

    import replicate

    b64 = base64.b64encode(photo.content).decode("ascii")
    data_uri = f"data:{photo.mime_type};base64,{b64}"
    model = get_model_name()

    try:
        output = replicate.run(
            model,
            input={"image": data_uri, "auto_rotate": True, "remove_alpha": True},
        )
    except Exception as e:
        raise DepthProviderError(_humanize_replicate_error(model, e)) from e

    try:
        depth_bytes, focal_px = _extract_depth_output(output)
        depth_map = _decode_metric_depth(depth_bytes)
        h, w = depth_map.shape
        fx = focal_px if focal_px else _focal_from_fov(w)
        return depth_map, fx, fx  # assume square pixels (fy == fx)
    except DepthProviderError:
        raise
    except Exception as e:
        raise DepthProviderError(
            f"Could not decode depth output from '{model}' "
            f"({type(e).__name__}: {e})"
        ) from e


def _extract_depth_output(output: Any) -> tuple[bytes, float | None]:
    """Normalize replicate.run's output to (depth_file_bytes, focal_px_or_None).
    Raises DepthProviderError if the output is a visualization image (no metric
    values) rather than a metric depth file."""
    focal_px: float | None = None
    depth_obj: Any = output

    if isinstance(output, dict):
        focal = output.get("focallength_px") or output.get("focal_length_px")
        focal_px = float(focal) if focal else None
        depth_obj = (
            output.get("depth") or output.get("depth_npz") or output.get("depth_raw")
        )
        if depth_obj is None:
            raise DepthProviderError(
                "Depth model returned a dict without a 'depth' file. Point DEPTH_MODEL "
                "at a cog that returns per-pixel metric depth."
            )
    elif isinstance(output, (list, tuple)):
        depth_obj = output[0] if output else None

    depth_bytes = _read_file_output(depth_obj)
    if _looks_like_plain_image(depth_bytes):
        raise DepthProviderError(
            f"DEPTH_MODEL ('{get_model_name()}') returned a visualization image, not "
            "metric depth. Absolute mm sizing needs a model that returns per-pixel "
            "metric depth (meters) + focal length — e.g. your own Depth Pro cog. "
            "Set DEPTH_MODEL accordingly, or use DEPTH_PROVIDER=none."
        )
    return depth_bytes, focal_px


def _read_file_output(obj: Any) -> bytes:
    """Get raw bytes from whatever replicate.run returned for a file: a
    FileOutput (.read()), an object with a .url, or a plain URL string."""
    if obj is None:
        raise DepthProviderError("Depth model returned no output.")
    if hasattr(obj, "read"):
        return obj.read()
    url = getattr(obj, "url", None) or (obj if isinstance(obj, str) else None)
    if url:
        import httpx

        resp = httpx.get(url, timeout=60.0)
        resp.raise_for_status()
        return resp.content
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj)
    raise DepthProviderError(f"Unrecognized depth output type: {type(obj).__name__}")


def _looks_like_plain_image(data: bytes) -> bool:
    """True for PNG/JPEG magic bytes — those are visualizations, not metric
    depth. (16-bit TIFF/EXR/NPZ metric files have different magic and pass.)"""
    return data[:8].startswith(b"\x89PNG") or data[:3] == b"\xff\xd8\xff"


def _decode_metric_depth(data: bytes):
    """Decode a metric depth file (npz/npy first, then 16-bit TIFF) into an HxW
    float array of meters."""
    import io

    import numpy as np

    # numpy .npy / .npz
    try:
        loaded = np.load(io.BytesIO(data), allow_pickle=False)
        if hasattr(loaded, "files"):  # npz
            arr = loaded[loaded.files[0]]
        else:
            arr = loaded
        return np.asarray(arr, dtype=float)
    except Exception:
        pass

    # Fall back to an image loader for TIFF/EXR-style single-channel metric depth.
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    arr = np.asarray(img, dtype=float)
    if arr.ndim == 3:
        arr = arr[..., 0]
    return arr


def _focal_from_fov(width_px: int) -> float:
    return (width_px / 2.0) / math.tan(math.radians(_ASSUMED_HFOV_DEG) / 2.0)


def _humanize_replicate_error(model: str, exc: Exception) -> str:
    """Map a raw replicate error to something actionable without importing the
    SDK's exception classes (robust to version changes). Replicate's
    ReplicateError carries an HTTP `.status`; ModelError means the prediction
    itself crashed."""
    status = getattr(exc, "status", None)
    cls = type(exc).__name__
    # str(exc) can itself raise for a pathological exception — never let that
    # escape the wrapper.
    try:
        text = str(exc) or cls
    except Exception:
        text = cls

    def msg(reason: str) -> str:
        return f"depth model '{model}' request failed: {reason} ({cls}: {text})"

    if status == 401 or "auth" in text.lower():
        return msg("authentication failed — check REPLICATE_API_TOKEN")
    if status == 404:
        return msg("model not found — check DEPTH_MODEL")
    if status == 429:
        return msg("rate limited — retry later")
    if "modelerror" in cls.lower():
        return msg("the model's own prediction crashed")
    if (
        "connection" in cls.lower()
        or "timeout" in cls.lower()
        or "timed out" in text.lower()
    ):
        return msg("could not reach Replicate (network error/timeout)")
    if status is not None:
        return msg(f"Replicate returned HTTP {status}")
    return msg("unexpected depth-provider error")

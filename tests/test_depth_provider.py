"""Tests for api/depth_provider.py: provider/model selection, the fail-fast
check, the "none" path (the product must work fully without depth), the pure
metric geometry (against synthetic depth maps — no network), and the Replicate
decode/error-wrapping (with `replicate` mocked at import). No real network.
"""

from __future__ import annotations

import io
import sys
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from api.depth_provider import (
    DepthProviderError,
    ScaleEstimate,
    ScaleRegion,
    _decode_metric_depth,
    _focal_from_fov,
    _looks_like_plain_image,
    _region_confidence,
    _region_size_mm,
    check_provider_configured,
    estimate_scale,
    get_model_name,
    get_provider_name,
)
from api.photo import PhotoInput


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("DEPTH_PROVIDER", "DEPTH_MODEL", "REPLICATE_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Provider / model selection + fail-fast check
# ---------------------------------------------------------------------------


def test_default_provider_is_none():
    assert get_provider_name() == "none"


def test_provider_override(monkeypatch):
    monkeypatch.setenv("DEPTH_PROVIDER", "  Replicate ")
    assert get_provider_name() == "replicate"


def test_default_and_overridden_model(monkeypatch):
    assert get_model_name() == "garg-aayush/ml-depth-pro"
    monkeypatch.setenv("DEPTH_MODEL", "me/metric-depth")
    assert get_model_name() == "me/metric-depth"


def test_check_none_never_needs_anything():
    check_provider_configured("none")  # must not raise


def test_check_replicate_without_token_raises(monkeypatch):
    monkeypatch.setenv("DEPTH_PROVIDER", "replicate")
    with pytest.raises(DepthProviderError, match="REPLICATE_API_TOKEN"):
        check_provider_configured()


def test_check_replicate_ignores_whitespace_only_token(monkeypatch):
    monkeypatch.setenv("DEPTH_PROVIDER", "replicate")
    monkeypatch.setenv("REPLICATE_API_TOKEN", "   ")
    with pytest.raises(DepthProviderError):
        check_provider_configured()


def test_check_replicate_with_token_ok(monkeypatch):
    monkeypatch.setenv("DEPTH_PROVIDER", "replicate")
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r8_real")
    check_provider_configured()  # must not raise


def test_check_unknown_provider_raises(monkeypatch):
    monkeypatch.setenv("DEPTH_PROVIDER", "bogus")
    with pytest.raises(DepthProviderError):
        check_provider_configured()


# ---------------------------------------------------------------------------
# The "none" path — the product must work fully without depth
# ---------------------------------------------------------------------------


def test_none_provider_returns_no_estimates():
    regions = [ScaleRegion("span_mm", "arrow", [[0.1, 0.5], [0.9, 0.5]])]
    assert estimate_scale(PhotoInput(b"x"), regions) == []


def test_estimate_scale_unknown_provider_raises(monkeypatch):
    monkeypatch.setenv("DEPTH_PROVIDER", "bogus")
    with pytest.raises(DepthProviderError):
        estimate_scale(PhotoInput(b"x"), [ScaleRegion("d", "arrow", [[0, 0], [1, 1]])])


# ---------------------------------------------------------------------------
# Pure metric geometry (synthetic depth maps, no network)
# ---------------------------------------------------------------------------


def test_region_size_matches_pinhole_math():
    w, h = 1000, 750
    depth = np.full((h, w), 1.0)  # 1 metre everywhere
    fx = _focal_from_fov(w)
    region = ScaleRegion("span_mm", "arrow", [[0.1, 0.5], [0.9, 0.5]])
    size = _region_size_mm(depth, fx, fx, region)
    # analytic: horizontal pixel span * (depth / fx), in mm
    expected = (0.9 - 0.1) * (w - 1) / fx * 1.0 * 1000.0
    assert size == pytest.approx(expected, rel=1e-6)


def test_region_size_scales_with_depth():
    w, h = 640, 480
    fx = 800.0
    region = ScaleRegion("span_mm", "arrow", [[0.2, 0.5], [0.8, 0.5]])
    near = _region_size_mm(np.full((h, w), 0.5), fx, fx, region)
    far = _region_size_mm(np.full((h, w), 2.0), fx, fx, region)
    # 4x the depth => 4x the real-world size for the same pixel span
    assert far == pytest.approx(near * 4.0, rel=1e-6)


def test_single_point_region_is_unmeasurable():
    depth = np.full((100, 100), 1.0)
    assert (
        _region_size_mm(depth, 500.0, 500.0, ScaleRegion("d", "circle", [[0.5, 0.5]]))
        is None
    )


def test_invalid_depth_returns_none():
    depth = np.zeros((100, 100))  # no valid depth
    region = ScaleRegion("d", "arrow", [[0.1, 0.5], [0.9, 0.5]])
    assert _region_size_mm(depth, 500.0, 500.0, region) is None


def test_confidence_is_capped_and_modest():
    depth = np.full((100, 100), 1.0)
    region = ScaleRegion("d", "arrow", [[0.1, 0.5], [0.9, 0.5]])
    conf = _region_confidence(depth, region)
    assert 0 < conf <= 0.5


# ---------------------------------------------------------------------------
# Replicate decode + error wrapping (replicate mocked at import)
# ---------------------------------------------------------------------------


class _FileOutput:
    """Stand-in for replicate's FileOutput (has .read())."""

    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data


def _png_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(buf, "PNG")
    return buf.getvalue()


def _npz_depth_bytes(depth: np.ndarray) -> bytes:
    buf = io.BytesIO()
    np.savez(buf, depth=depth)
    return buf.getvalue()


def _replicate_env(monkeypatch):
    monkeypatch.setenv("DEPTH_PROVIDER", "replicate")
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r8_real")


def test_looks_like_plain_image_detects_png_and_jpeg():
    assert _looks_like_plain_image(_png_bytes())
    assert _looks_like_plain_image(b"\xff\xd8\xff\xe0rest")
    assert not _looks_like_plain_image(_npz_depth_bytes(np.ones((4, 4))))


def test_decode_metric_depth_from_npz():
    arr = _decode_metric_depth(_npz_depth_bytes(np.full((5, 7), 2.5)))
    assert arr.shape == (5, 7)
    assert arr[0, 0] == pytest.approx(2.5)


def test_replicate_rejects_visualization_image(monkeypatch):
    _replicate_env(monkeypatch)
    fake = MagicMock()
    fake.run.return_value = _FileOutput(_png_bytes())
    with patch.dict(sys.modules, {"replicate": fake}):
        with pytest.raises(DepthProviderError, match="visualization image"):
            estimate_scale(
                PhotoInput(b"jpeg"),
                [ScaleRegion("span_mm", "arrow", [[0.1, 0.5], [0.9, 0.5]])],
            )


def test_replicate_metric_output_produces_estimate(monkeypatch):
    _replicate_env(monkeypatch)
    h, w = 480, 640
    depth = np.full((h, w), 0.5)  # 0.5 m
    fake = MagicMock()
    fake.run.return_value = {
        "depth": _FileOutput(_npz_depth_bytes(depth)),
        "focallength_px": 800.0,
    }
    with patch.dict(sys.modules, {"replicate": fake}):
        estimates = estimate_scale(
            PhotoInput(b"jpeg"),
            [ScaleRegion("span_mm", "arrow", [[0.2, 0.5], [0.8, 0.5]])],
        )
    assert len(estimates) == 1
    est = estimates[0]
    assert isinstance(est, ScaleEstimate)
    assert est.dim_name == "span_mm"
    expected = (0.8 - 0.2) * (w - 1) / 800.0 * 0.5 * 1000.0
    assert est.value_mm == pytest.approx(expected, rel=1e-3)


def test_replicate_api_error_is_wrapped(monkeypatch):
    _replicate_env(monkeypatch)
    err = Exception("unauthorized")
    err.status = 401
    fake = MagicMock()
    fake.run.side_effect = err
    with patch.dict(sys.modules, {"replicate": fake}):
        with pytest.raises(DepthProviderError, match="authentication failed"):
            estimate_scale(
                PhotoInput(b"x"), [ScaleRegion("d", "arrow", [[0, 0], [1, 1]])]
            )


def test_replicate_import_error_becomes_depth_error(monkeypatch):
    """Regression (review finding #2): a missing/broken `replicate` (or numpy/
    PIL/httpx) install must surface as DepthProviderError so intent creation can
    degrade gracefully — never a raw ImportError leaking to a bare 500."""
    _replicate_env(monkeypatch)
    with patch.dict(
        sys.modules, {"replicate": None}
    ):  # `import replicate` -> ImportError
        with pytest.raises(DepthProviderError):
            estimate_scale(
                PhotoInput(b"x"), [ScaleRegion("d", "arrow", [[0, 0], [1, 1]])]
            )


def test_check_ignores_inline_comment_token(monkeypatch):
    """Regression (review finding #5): a python-dotenv inline-comment value
    ('# comment') must count as an unset token so the fail-fast still fires."""
    monkeypatch.setenv("DEPTH_PROVIDER", "replicate")
    monkeypatch.setenv("REPLICATE_API_TOKEN", "# needed from Milestone 4")
    with pytest.raises(DepthProviderError, match="REPLICATE_API_TOKEN"):
        check_provider_configured()


# ---------------------------------------------------------------------------
# Provider isolation: only api/depth_provider.py may import `replicate`.
# ---------------------------------------------------------------------------


def test_only_depth_provider_imports_replicate():
    import pathlib
    import re

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    pattern = re.compile(r"^\s*(import replicate\b|from replicate\b)")
    offenders = []
    for path in repo_root.rglob("*.py"):
        if ".venv" in path.parts:
            continue
        if path.name in ("depth_provider.py", "test_depth_provider.py"):
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if pattern.match(line):
                offenders.append(
                    f"{path.relative_to(repo_root)}:{lineno}: {line.strip()}"
                )
    assert (
        not offenders
    ), "only api/depth_provider.py may import replicate:\n" + "\n".join(offenders)

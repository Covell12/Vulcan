"""Tests for api/composite.py — the in-photo ghost preview (M5.5).

The camera math is verified against analytic pinhole cases (no image diffing):
a point on the optical axis lands on the principal point; a known offset lands
at the algebraically-predicted pixel; a unit cube at a known pose projects
symmetrically. The canonical mounting rotations are checked for being proper
rotations. Finally, render_composite is exercised end-to-end on a real template
mesh + a synthetic photo, with and without an annotation.
"""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from cadquery import exporters
from PIL import Image

from api import composite as C
from templates_lib.registry import get_template

# ---------------------------------------------------------------------------
# Pure pinhole projection
# ---------------------------------------------------------------------------


def test_optical_axis_projects_to_principal_point():
    pt = np.array([[0.0, 0.0, 1234.0]])
    uv = C.pinhole_project(pt, fx=800, fy=800, cx=320, cy=240)
    assert uv[0][0] == 320.0
    assert uv[0][1] == 240.0


def test_offset_projects_to_algebraic_pixel():
    # u = fx * X/Z + cx = 1000 * 100/1000 + 500 = 600 ; v = 500 - 50 = 450
    pt = np.array([[100.0, -50.0, 1000.0]])
    uv = C.pinhole_project(pt, fx=1000, fy=1000, cx=500, cy=500)
    assert np.allclose(uv[0], [600.0, 450.0])


def test_unit_cube_projects_symmetrically_about_center():
    cube = np.array(
        [[x, y, z] for x in (-0.5, 0.5) for y in (-0.5, 0.5) for z in (-0.5, 0.5)]
    )
    verts_cam = C.transform_to_camera(cube, np.eye(3), np.array([0.0, 0.0, 5.0]))
    uv = C.pinhole_project(verts_cam, fx=800, fy=800, cx=320, cy=240)
    # Symmetric in x about cx and in y about cy.
    assert np.isclose(uv[:, 0].min() + uv[:, 0].max(), 2 * 320.0)
    assert np.isclose(uv[:, 1].min() + uv[:, 1].max(), 2 * 240.0)
    # Nearer face (smaller Z) is larger on screen than the far face.
    near = C.pinhole_project(
        C.transform_to_camera(
            np.array([[0.5, 0.5, -0.5]]), np.eye(3), np.array([0.0, 0.0, 5.0])
        ),
        800,
        800,
        320,
        240,
    )
    far = C.pinhole_project(
        C.transform_to_camera(
            np.array([[0.5, 0.5, 0.5]]), np.eye(3), np.array([0.0, 0.0, 5.0])
        ),
        800,
        800,
        320,
        240,
    )
    assert near[0][0] > far[0][0]  # near corner is farther from center


def test_transform_matches_manual_rigid_transform():
    verts = np.array([[1.0, 2.0, 3.0], [-1.0, 0.0, 4.0]])
    R = C.canonical_rotation("surface")
    t = np.array([10.0, 20.0, 30.0])
    got = C.transform_to_camera(verts, R, t)
    for i, v in enumerate(verts):
        assert np.allclose(got[i], R @ v + t)


# ---------------------------------------------------------------------------
# Focal length
# ---------------------------------------------------------------------------


def test_focal_from_fov_when_no_exif():
    # 60 deg HFOV: fx = (W/2)/tan(30deg)
    f = C.focal_px(1000, exif_focal_35mm=None)
    assert np.isclose(f, 500.0 / np.tan(np.radians(30.0)))


def test_focal_from_exif_35mm_equivalent():
    # 28mm-equivalent on a 4000px-wide frame: fx = 28/36 * 4000
    f = C.focal_px(4000, exif_focal_35mm=28.0)
    assert np.isclose(f, 28.0 / 36.0 * 4000)


# ---------------------------------------------------------------------------
# Canonical rotations
# ---------------------------------------------------------------------------


def test_canonical_rotations_are_proper():
    for mounting in ("wall", "surface"):
        R = C.canonical_rotation(mounting)
        assert np.allclose(R @ R.T, np.eye(3), atol=1e-9)
        assert np.isclose(np.linalg.det(R), 1.0)


def test_mounting_for_category():
    assert C.mounting_for_category("bracket") == "wall"
    assert C.mounting_for_category("hook") == "wall"
    assert C.mounting_for_category("knob") == "surface"
    assert C.mounting_for_category("adapter") == "surface"
    assert C.mounting_for_category(None) == "surface"
    assert C.mounting_for_category("unknown-cat") == "surface"


# ---------------------------------------------------------------------------
# Annotation parsing
# ---------------------------------------------------------------------------


def test_annotation_centroid_and_none():
    ann = [{"photo_index": 0, "points": [[0.2, 0.4], [0.4, 0.6]]}]
    cx, cy = C.annotation_centroid(ann)
    assert np.isclose(cx, 0.3) and np.isclose(cy, 0.5)
    assert C.annotation_centroid(None) is None
    assert C.annotation_centroid([]) is None
    assert C.annotation_centroid([{"photo_index": 1, "points": [[0.5, 0.5]]}]) is None
    # M5.5 review: a malformed/scalar `points` field must be tolerated, not raise.
    assert C.annotation_centroid([{"photo_index": 0, "points": 42}]) is None
    assert C.annotation_centroid([{"photo_index": 0}]) is None
    assert C.annotation_centroid("not-a-list") is None


def test_anchor_extent_falls_back_to_center_without_annotation():
    (ax, ay), extent = C._anchor_and_extent(None, 1000, 800)
    assert (ax, ay) == (500.0, 400.0)
    assert extent is None


def test_anchor_depth_prefers_depth_then_annotation_then_fallback():
    # depth wins outright
    assert C._anchor_depth_mm(100, 1000, 1000, extent_px=200, depth_mm=777) == 777
    # annotation extent: Z = size_mm * fx / extent_px
    z = C._anchor_depth_mm(100, 1000, 1000, extent_px=200, depth_mm=None)
    assert np.isclose(z, 100 * 1000 / 200)
    # neither: fixed fraction of the frame
    z2 = C._anchor_depth_mm(100, 1000, 1000, extent_px=None, depth_mm=None)
    assert np.isclose(z2, 100 * 1000 / (C._FALLBACK_FRAME_FRACTION * 1000))


# ---------------------------------------------------------------------------
# End-to-end render
# ---------------------------------------------------------------------------


def _photo_bytes(w: int = 1200, h: int = 900) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (190, 195, 200)).save(buf, "JPEG")
    return buf.getvalue()


def _bracket_stl(tmp_path: Path) -> Path:
    spec = get_template("bracket_shelf_l")
    solid = spec.build_fn(spec.params_model(span_mm=180, depth_mm=60))
    stl = tmp_path / "part.stl"
    exporters.export(solid, str(stl))
    return stl


def test_render_composite_with_annotation(tmp_path: Path):
    stl = _bracket_stl(tmp_path)
    out = tmp_path / "composite.png"
    ann = [{"photo_index": 0, "points": [[0.45, 0.5], [0.6, 0.62]]}]
    C.render_composite(_photo_bytes(), stl, out, category="bracket", annotation=ann)
    assert out.exists() and out.stat().st_size > 0
    img = Image.open(out)
    assert img.size[0] <= C._MAX_DIM_PX and img.size[1] <= C._MAX_DIM_PX


def test_render_composite_without_annotation(tmp_path: Path):
    stl = _bracket_stl(tmp_path)
    out = tmp_path / "composite.png"
    C.render_composite(_photo_bytes(), stl, out, category="knob", annotation=None)
    assert out.exists() and out.stat().st_size > 0


def test_render_composite_actually_draws_the_ghost(tmp_path: Path):
    """The composite must differ from the untouched photo — i.e. the ghost is
    actually painted, not a no-op copy."""
    stl = _bracket_stl(tmp_path)
    out = tmp_path / "composite.png"
    photo = _photo_bytes()
    ann = [{"photo_index": 0, "points": [[0.4, 0.45], [0.62, 0.6]]}]
    C.render_composite(photo, stl, out, category="bracket", annotation=ann)

    original = np.asarray(Image.open(io.BytesIO(photo)).convert("RGB"), dtype=int)
    composited = np.asarray(
        Image.open(out).convert("RGB").resize((original.shape[1], original.shape[0])),
        dtype=int,
    )
    changed = np.abs(original - composited).sum(axis=2) > 10
    assert changed.mean() > 0.01, "ghost overlay barely changed the photo"


def test_render_composite_is_opaque_ember(tmp_path: Path):
    """The part is drawn OPAQUE in the ember family (not a translucent blue
    smear): the photo has meaningful solid orange/ember pixels, and there's a
    glowing border of intermediate orange around it."""
    stl = _bracket_stl(tmp_path)
    out = tmp_path / "composite.png"
    # A flat gray photo so any orange comes only from the part + its glow.
    photo = io.BytesIO()
    Image.new("RGB", (600, 450), (70, 70, 74)).save(photo, "JPEG")
    ann = [{"photo_index": 0, "points": [[0.35, 0.4], [0.65, 0.62]]}]
    C.render_composite(photo.getvalue(), stl, out, category="bracket", annotation=ann)
    arr = np.asarray(Image.open(out).convert("RGB"), dtype=int)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    # Opaque ember body: strong red, mid green, low blue (unlike the old blue ghost).
    ember = (r > 170) & (g > 55) & (g < 175) & (b < 110)
    assert ember.sum() > 400, "expected an opaque ember-coloured part"
    # A glow halo: orange-ish pixels that are NOT the flat gray background.
    glow = (r > 110) & (r - b > 40) & ~ember
    assert glow.sum() > 200, "expected a glowing orange border around the part"

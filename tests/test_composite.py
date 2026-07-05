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
    """The part is drawn OPAQUE and ORANGE (ember family, not a translucent blue
    smear). The lighting match may DIM it toward a dark scene, so we assert the
    hue ordering (r > g > b, red clearly dominant) rather than a fixed brightness —
    ember identity must survive the tint/dim."""
    stl = _bracket_stl(tmp_path)
    out = tmp_path / "composite.png"
    photo = io.BytesIO()
    Image.new("RGB", (600, 450), (70, 70, 74)).save(photo, "JPEG")
    ann = [{"photo_index": 0, "points": [[0.35, 0.4], [0.65, 0.62]]}]
    C.render_composite(photo.getvalue(), stl, out, category="bracket", annotation=ann)
    arr = np.asarray(Image.open(out).convert("RGB"), dtype=int)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    # Opaque ember body: orange, red clearly dominant over blue, g between.
    ember = (r > 120) & (r - b > 55) & (g < r) & (g > b)
    assert ember.sum() > 400, "expected an opaque ember-coloured part"
    # A brighter glow halo (the glow is ember-identity, NOT dimmed by lighting).
    glow = (r > 175) & (r - b > 70)
    assert glow.sum() > 120, "expected a glowing orange border around the part"


# ---------------------------------------------------------------------------
# M10b: scene occlusion, lighting match, contact shadow
# ---------------------------------------------------------------------------


def _ember_mask(arr: np.ndarray) -> np.ndarray:
    r, g, b = (
        arr[:, :, 0].astype(int),
        arr[:, :, 1].astype(int),
        arr[:, :, 2].astype(int),
    )
    return (r > 120) & (r - b > 45) & (g < r)


def test_scene_occlusion_hides_occluded_region(tmp_path: Path):
    """With a scene depth map whose LEFT half is nearer than the part, the part's
    left half is occluded (mostly gone) while the right half stays visible."""
    stl = _bracket_stl(tmp_path)
    ann = [{"photo_index": 0, "points": [[0.4, 0.4], [0.62, 0.62]]}]
    photo = _photo_bytes(600, 450)

    C.render_composite(
        photo, stl, tmp_path / "noocc.png", category="bracket", annotation=ann
    )
    base = _ember_mask(np.asarray(Image.open(tmp_path / "noocc.png").convert("RGB")))

    H, W = 450, 600
    depth = np.full((H, W), np.inf)
    depth[:, : W // 2] = 1.0  # 1 mm — nearer than the part → occludes the left half
    C.render_composite(
        photo,
        stl,
        tmp_path / "occ.png",
        category="bracket",
        annotation=ann,
        scene_depth_mm=depth,
    )
    occ = _ember_mask(np.asarray(Image.open(tmp_path / "occ.png").convert("RGB")))

    half = W // 2
    assert base[:, :half].sum() > 500  # there WAS a left-half part to hide
    assert occ[:, :half].sum() < 0.25 * base[:, :half].sum()  # left half occluded
    assert occ[:, half:].sum() > 0.6 * base[:, half:].sum()  # right half kept


def test_scene_occlusion_absent_degrades_to_full_front(tmp_path: Path):
    """No depth map → the part is drawn fully in front (graceful default)."""
    stl = _bracket_stl(tmp_path)
    ann = [{"photo_index": 0, "points": [[0.4, 0.4], [0.62, 0.62]]}]
    out = tmp_path / "c.png"
    C.render_composite(
        _photo_bytes(),
        stl,
        out,
        category="bracket",
        annotation=ann,
        scene_depth_mm=None,
    )
    assert _ember_mask(np.asarray(Image.open(out).convert("RGB"))).sum() > 500


def test_lighting_match_dims_part_in_dark_scene(tmp_path: Path):
    """The same part is dimmer in a dark scene than a bright one (brightness is
    matched to the photo), while staying ember."""
    stl = _bracket_stl(tmp_path)
    ann = [{"photo_index": 0, "points": [[0.4, 0.4], [0.62, 0.62]]}]

    def median_ember_r(bg):
        buf = io.BytesIO()
        Image.new("RGB", (600, 450), bg).save(buf, "JPEG")
        out = tmp_path / f"l{bg[0]}.png"
        C.render_composite(buf.getvalue(), stl, out, category="bracket", annotation=ann)
        arr = np.asarray(Image.open(out).convert("RGB"))
        m = _ember_mask(arr)
        return float(np.median(arr[:, :, 0][m]))

    dark_r = median_ember_r((25, 25, 28))
    bright_r = median_ember_r((235, 235, 235))
    assert (
        bright_r > dark_r + 20
    ), f"part should brighten in a bright scene ({dark_r} vs {bright_r})"


def test_scene_lighting_estimate_clamps():
    """scene_lighting clamps brightness to 0.6..1.3 for extreme scenes."""
    dark = np.full((40, 40, 3), 5.0)
    bright = np.full((40, 40, 3), 250.0)
    b_dark, _ = C.scene_lighting(dark, (20, 20), 15)
    b_bright, _ = C.scene_lighting(bright, (20, 20), 15)
    assert abs(b_dark - 0.6) < 1e-6 and abs(b_bright - 1.3) < 1e-6


def test_contact_shadow_darkens_around_part(tmp_path: Path):
    """A soft contact shadow darkens pixels around the part below the flat
    background (they're not part pixels — it's the cast shadow)."""
    stl = _bracket_stl(tmp_path)
    ann = [{"photo_index": 0, "points": [[0.4, 0.4], [0.62, 0.62]]}]
    bg = 185
    buf = io.BytesIO()
    Image.new("RGB", (600, 450), (bg, bg, bg)).save(buf, "JPEG")
    out = tmp_path / "shadow.png"
    C.render_composite(buf.getvalue(), stl, out, category="knob", annotation=ann)
    arr = np.asarray(Image.open(out).convert("RGB"), dtype=int)
    lum = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    dark_non_part = (lum < bg - 25) & ~_ember_mask(arr)
    assert dark_non_part.sum() > 150, "expected a contact shadow darkening the ground"

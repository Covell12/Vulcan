"""Ghost composite (M5.5): render the ACTUAL generated geometry back into the
user's own photo, scale-true and position-true — a deliberately synthetic,
semi-transparent "ghost" of the part where it will go.

Why this exists: the callout render (api/rendering.py) shows the part in the
abstract; this shows it *in situ*, at roughly the right size and place, so the
user can sanity-check "yes, that bracket is about that big, right there" before
paying. The part is drawn as an OPAQUE, flat-shaded solid in the Vulcan ember
colour with a glowing orange border, so it reads as a real object dropped into
the scene (not a translucent smear) — while the fixed colour + synthetic shading
still keep it clearly a preview, not a photo of a finished part.

Deliberately dependency-light: pure numpy + Pillow + trimesh, NO OpenGL /
pyrender / GPU. It runs in the same headless API process as everything else.
The projection is a textbook pinhole camera; triangles are painted back-to-
front (painter's algorithm). That's plenty for a preview and keeps the math
unit-testable (see tests/test_composite.py).

HONESTY / v0 LIMITATIONS (surfaced to the user in the UI copy):
  - ORIENTATION is canonical, not recovered from the photo. We do not know the
    real wall/surface plane, so we place the part in a fixed, recognizable 3/4
    pose chosen only by the template's mounting category (wall vs. surface).
  - SCALE prefers metric depth at the circled point (DEPTH_PROVIDER); with no
    depth it is inferred from the part's own size vs. how big the user's
    annotation is, and with neither it falls back to a fixed fraction of frame.
  - PLACEMENT anchors the part's centroid at the annotation centroid (or the
    photo center). No contact/gravity solve.
  - OCCLUSION (part hidden behind foreground objects) needs a metric depth map of
    the WHOLE scene, which requires a depth model; the current depth provider only
    returns depth at the single circled point (for scale), so occlusion is not yet
    applied — the part is drawn fully in front. A documented next step.
Treat the preview as "about this big, about here", not a measurement.
"""

from __future__ import annotations

import io
import math
from pathlib import Path
from typing import Any

import numpy as np
import trimesh
from PIL import Image, ImageDraw

# Assumed horizontal field of view when the photo carries no EXIF focal length.
# Matches api/depth_provider._ASSUMED_HFOV_DEG so both subsystems make the same
# uncalibrated-camera assumption.
_HFOV_DEG = 60.0

# EXIF tag 41989 = FocalLengthIn35mmFilm (focal length in 35mm-equivalent mm).
# 36mm is the width of a full-frame (35mm) sensor, so f_px = f35/36 * width_px.
_EXIF_FOCAL_35MM_TAG = 41989
_FULL_FRAME_WIDTH_MM = 36.0

# The photo is downscaled so its longest side is at most this many pixels before
# compositing — keeps the preview PNG small and the rasterization fast without
# changing the (unitless) projection.
_MAX_DIM_PX = 1280

# With no depth and no annotation to scale against, make the part span this
# fraction of the frame width. A documented last-resort guess.
_FALLBACK_FRAME_FRACTION = 0.35

# Part styling: an OPAQUE, flat-shaded solid in the Vulcan ember family with a
# glowing orange border around its silhouette — reads as a real object dropped
# into the scene, not a translucent smear. `_PART_RGB` is the lit base color;
# per-face shading darkens it toward `_PART_SHADOW_RGB` on faces turned away from
# the camera/light so the form is legible.
_PART_RGB = (255, 122, 40)
_PART_SHADOW_RGB = (92, 34, 8)
_EDGE_RGBA = (40, 16, 4, 180)
_GLOW_RGB = (255, 140, 32)
_GLOW_RADIUS_PX = 7  # how far the orange halo spreads beyond the silhouette

# Which canonical mounting each template category gets. Wall-mounted parts stand
# against a vertical plane facing the camera; everything else sits on a surface.
_MOUNTING_BY_CATEGORY = {
    "bracket": "wall",
    "hook": "wall",
    "clip": "wall",
    "enclosure": "surface",
    "knob": "surface",
    "adapter": "surface",
    "other": "surface",
}


def mounting_for_category(category: str | None) -> str:
    """'wall' or 'surface' for a template category (default 'surface')."""
    return _MOUNTING_BY_CATEGORY.get(category or "", "surface")


# ---------------------------------------------------------------------------
# Pure camera math (unit-tested against analytic cases; no I/O)
# ---------------------------------------------------------------------------


def focal_px(width_px: int, exif_focal_35mm: float | None = None) -> float:
    """Focal length in pixels. Uses the EXIF 35mm-equivalent focal length when
    present (the calibrated path); otherwise derives one from an assumed
    horizontal field of view."""
    if exif_focal_35mm and exif_focal_35mm > 0:
        return exif_focal_35mm / _FULL_FRAME_WIDTH_MM * width_px
    return (width_px / 2.0) / math.tan(math.radians(_HFOV_DEG) / 2.0)


def pinhole_project(
    pts_cam: np.ndarray, fx: float, fy: float, cx: float, cy: float
) -> np.ndarray:
    """Project camera-space points (N,3), with +Z pointing INTO the scene, to
    pixel coordinates (N,2) through a pinhole with principal point (cx, cy).
    Depth is clamped to a tiny positive value so a point exactly on the image
    plane doesn't divide by zero (callers cull behind-camera faces separately)."""
    pts = np.asarray(pts_cam, dtype=float)
    z = np.clip(pts[:, 2], 1e-9, None)
    u = fx * pts[:, 0] / z + cx
    v = fy * pts[:, 1] / z + cy
    return np.stack([u, v], axis=1)


def transform_to_camera(
    verts: np.ndarray, rotation: np.ndarray, translation: np.ndarray
) -> np.ndarray:
    """Rigid transform of model-space vertices (N,3) into camera space:
    v_cam = R @ v_model + t, done row-wise as v @ R.T + t."""
    return np.asarray(verts, dtype=float) @ np.asarray(
        rotation, dtype=float
    ).T + np.asarray(translation, dtype=float)


def _rot_x(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=float)


def _rot_y(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], dtype=float)


def _basis_rotation(up_model: np.ndarray, front_model: np.ndarray) -> np.ndarray:
    """Rotation mapping the given model axes to the camera frame so `up_model`
    points up the image (camera -Y) and `front_model` points toward the camera
    (camera -Z). Camera frame is +X right, +Y down, +Z into the scene."""
    up_cam = np.array([0.0, -1.0, 0.0])
    fwd_cam = np.array([0.0, 0.0, -1.0])
    right_cam = np.cross(up_cam, fwd_cam)

    up_m = up_model / np.linalg.norm(up_model)
    front_m = front_model / np.linalg.norm(front_model)
    right_m = np.cross(up_m, front_m)
    right_m = right_m / np.linalg.norm(right_m)

    model_basis = np.stack([right_m, up_m, front_m], axis=1)  # columns
    cam_basis = np.stack([right_cam, up_cam, fwd_cam], axis=1)
    return cam_basis @ model_basis.T


def canonical_rotation(mounting: str) -> np.ndarray:
    """A fixed, recognizable 3/4 pose for the part, chosen only by mounting type.
    This is the honest v0 limitation: orientation is NOT recovered from the
    photo. Surface mounts stand on +Z (their height/axis) and are viewed from
    slightly above; wall mounts stand on +Y (the mounting arm) with the part
    projecting toward the camera, viewed nearly head-on."""
    if mounting == "wall":
        align = _basis_rotation(
            up_model=np.array([0.0, 1.0, 0.0]),
            front_model=np.array([1.0, 0.0, 0.0]),
        )
        view = _rot_x(math.radians(8.0)) @ _rot_y(math.radians(-25.0))
    else:  # surface
        align = _basis_rotation(
            up_model=np.array([0.0, 0.0, 1.0]),
            front_model=np.array([0.0, 1.0, 0.0]),
        )
        view = _rot_x(math.radians(20.0)) @ _rot_y(math.radians(-30.0))
    return view @ align


# ---------------------------------------------------------------------------
# Placement (turns a photo + annotation + optional depth into a camera pose)
# ---------------------------------------------------------------------------


def _annotation_points(annotation: Any, photo_index: int = 0) -> list[list[float]]:
    """All normalized [x, y] points the user drew on the given photo, flattened
    across annotation entries. Returns [] if there's no usable annotation."""
    pts: list[list[float]] = []
    if not isinstance(annotation, list):
        return pts
    for entry in annotation:
        if not isinstance(entry, dict):
            continue
        if entry.get("photo_index", 0) != photo_index:
            continue
        raw = entry.get("points")
        if not isinstance(raw, (list, tuple)):
            continue  # tolerate a malformed/scalar `points` field
        for p in raw:
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                pts.append([float(p[0]), float(p[1])])
    return pts


def annotation_centroid(annotation: Any) -> tuple[float, float] | None:
    """Normalized [x, y] centroid of everything the user drew on photo 0, or
    None if there's no annotation. Lets the caller ask the depth provider for
    metric depth at the exact point the user circled."""
    pts = _annotation_points(annotation)
    if not pts:
        return None
    arr = np.array(pts, dtype=float)
    return float(arr[:, 0].mean()), float(arr[:, 1].mean())


def _anchor_and_extent(
    annotation: Any, width_px: int, height_px: int
) -> tuple[tuple[float, float], float | None]:
    """From the annotation, return (anchor_pixel, extent_px):
    - anchor_pixel: centroid of the drawn points, or the image center.
    - extent_px: diagonal of the annotation's bounding box in pixels, or None
      when there aren't enough points to measure a size."""
    pts = _annotation_points(annotation)
    if not pts:
        return (width_px / 2.0, height_px / 2.0), None
    arr = np.array(pts, dtype=float)
    anchor = (float(arr[:, 0].mean()) * width_px, float(arr[:, 1].mean()) * height_px)
    if len(pts) < 2:
        return anchor, None
    span_x = (float(arr[:, 0].max()) - float(arr[:, 0].min())) * width_px
    span_y = (float(arr[:, 1].max()) - float(arr[:, 1].min())) * height_px
    extent = math.hypot(span_x, span_y)
    return anchor, (extent if extent > 1.0 else None)


def _anchor_depth_mm(
    part_size_mm: float,
    fx: float,
    width_px: int,
    extent_px: float | None,
    depth_mm: float | None,
) -> float:
    """Distance (mm) to place the part's centroid so it projects at a believable
    size. Preference order: true metric depth at the circled point → the part's
    own size vs. the annotation's on-screen size → a fixed fraction of frame."""
    if depth_mm and depth_mm > 0:
        return depth_mm
    if extent_px and extent_px > 0:
        # Place the part so its characteristic size projects to the annotation's
        # on-screen extent:  size_px = size_mm * fx / Z  ->  Z = size_mm*fx/size_px.
        return part_size_mm * fx / extent_px
    return part_size_mm * fx / (_FALLBACK_FRAME_FRACTION * width_px)


# ---------------------------------------------------------------------------
# Rasterization
# ---------------------------------------------------------------------------


def _load_photo(photo_bytes: bytes) -> tuple[Image.Image, float | None]:
    """Load the photo, honor its EXIF orientation, downscale to _MAX_DIM_PX, and
    return (RGB image, EXIF 35mm-equivalent focal length or None)."""
    from PIL import ImageOps

    img = Image.open(io.BytesIO(photo_bytes))
    exif_focal = None
    try:
        exif = img.getexif()
        raw = exif.get(_EXIF_FOCAL_35MM_TAG) if exif else None
        exif_focal = float(raw) if raw else None
    except Exception:
        exif_focal = None

    img = ImageOps.exif_transpose(img).convert("RGB")
    if max(img.size) > _MAX_DIM_PX:
        scale = _MAX_DIM_PX / max(img.size)
        img = img.resize(
            (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
            Image.LANCZOS,
        )
    return img, exif_focal


def _face_shades(verts_cam: np.ndarray, faces: np.ndarray) -> np.ndarray:
    """A 0..1 brightness per face from simple flat shading: normals oriented
    toward the camera, lit from the upper-left-front, so the solid reads as a 3D
    object rather than a flat blob. Pure numpy — no lighting model dependency."""
    tris = verts_cam[faces]  # (F,3,3)
    n = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    nl = np.linalg.norm(n, axis=1, keepdims=True)
    nl[nl == 0] = 1.0
    n = n / nl
    centers = tris.mean(axis=1)
    # Flip normals that point away from the camera (origin), so all face the viewer.
    away = np.sum(n * centers, axis=1) > 0
    n[away] = -n[away]
    facing = np.clip(-n[:, 2], 0.0, 1.0)  # 1 = square to camera
    left_light = np.clip(-n[:, 0], 0.0, 1.0)  # a touch brighter on left-facing
    return np.clip(0.42 + 0.5 * facing + 0.14 * left_light, 0.0, 1.0)


def _rasterize(
    base: Image.Image,
    verts_cam: np.ndarray,
    faces: np.ndarray,
    fx: float,
    fy: float,
    cx: float,
    cy: float,
) -> Image.Image:
    """Paint the mesh as an OPAQUE, flat-shaded solid onto `base`, back-to-front
    (so nearer faces cover farther ones), then add a glowing orange halo around
    its silhouette. Faces with any vertex at or behind the camera are skipped."""
    from PIL import ImageFilter

    verts_2d = pinhole_project(verts_cam, fx, fy, cx, cy)
    face_z = verts_cam[faces][:, :, 2]  # (F,3) camera depth per face vertex

    keep = np.all(face_z > 1e-6, axis=1)
    order = np.argsort(-face_z.mean(axis=1))
    order = order[keep[order]]

    shades = _face_shades(verts_cam, faces)
    part = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(part)
    lit = np.array(_PART_RGB, dtype=float)
    shadow = np.array(_PART_SHADOW_RGB, dtype=float)
    for fi in order:
        tri = verts_2d[faces[fi]]
        poly = [(float(p[0]), float(p[1])) for p in tri]
        s = float(shades[fi])
        col = tuple(int(round(shadow[i] + (lit[i] - shadow[i]) * s)) for i in range(3))
        draw.polygon(poly, fill=col + (255,), outline=_EDGE_RGBA)

    # Glowing orange border: blur the part's silhouette and colour the spill.
    alpha = part.split()[3]
    halo = alpha.filter(ImageFilter.GaussianBlur(_GLOW_RADIUS_PX))
    glow = Image.new("RGBA", base.size, _GLOW_RGB + (0,))
    glow.putalpha(halo)

    out = base.convert("RGBA")
    out = Image.alpha_composite(out, glow)  # halo sits under the part
    out = Image.alpha_composite(out, glow)  # doubled → a brighter, real "glow"
    out = Image.alpha_composite(out, part)  # opaque part on top
    return out.convert("RGB")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def render_composite(
    photo_bytes: bytes,
    stl_path: Path,
    out_path: Path,
    *,
    category: str | None,
    annotation: Any = None,
    depth_mm: float | None = None,
) -> Path:
    """Render `stl_path`'s geometry into `photo_bytes` and write a PNG to
    `out_path`. Returns out_path. Raises on unreadable inputs — the caller (the
    design join) treats the composite as best-effort and swallows failures so a
    preview problem never blocks delivering the actual part files."""
    mesh = trimesh.load(str(stl_path), force="mesh")
    verts = np.asarray(mesh.vertices, dtype=float)
    faces = np.asarray(mesh.faces)
    if verts.size == 0 or faces.size == 0:
        raise ValueError(f"mesh at {stl_path} has no geometry to composite")

    photo, exif_focal = _load_photo(photo_bytes)
    W, H = photo.width, photo.height
    fx = fy = focal_px(W, exif_focal)
    cx, cy = W / 2.0, H / 2.0

    bounds = mesh.bounds
    part_center = (bounds[0] + bounds[1]) / 2.0
    part_size_mm = float(np.max(bounds[1] - bounds[0])) or 1.0

    (anchor_u, anchor_v), extent_px = _anchor_and_extent(annotation, W, H)
    z_anchor = _anchor_depth_mm(part_size_mm, fx, W, extent_px, depth_mm)

    # Back-project the anchor pixel to its 3D camera-space position at z_anchor,
    # then place the part's centroid there.
    anchor_cam = np.array(
        [
            (anchor_u - cx) * z_anchor / fx,
            (anchor_v - cy) * z_anchor / fy,
            z_anchor,
        ]
    )
    rotation = canonical_rotation(mounting_for_category(category))
    verts_cam = transform_to_camera(verts - part_center, rotation, anchor_cam)

    result = _rasterize(photo, verts_cam, faces, fx, fy, cx, cy)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result.save(str(out_path))
    # Also save the plain (EXIF-corrected, downscaled) photo next to the
    # composite, so the UI can toggle the part in/out of the picture without
    # re-serving the private original from data/.
    photo.save(str(out_path.with_name("photo.png")))
    return out_path

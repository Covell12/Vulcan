"""Shared software rasterizer for Vulcan's previews (M10b).

A dependency-light, per-pixel Z-BUFFER triangle rasterizer — vectorized numpy,
NO OpenGL / pyrender / GPU (same headless constraint as the rest of the API). It
replaces the old painter's-algorithm compositing, which drew triangles back-to-
front and so mis-ordered self-occluding parts and assemblies (internal faces bled
through, edges z-fought).

Both the in-photo ghost composite (api/composite.py) and the studio product shot
(api/rendering.py) render through `render_mesh` here, so they share one correct
look.

How it works:
  - triangles are projected to pixels by the caller (pinhole); we get 2D vertices,
    a per-vertex camera depth, and a per-FACE colour (the caller does the shading);
  - for each triangle we walk only its pixel bounding box, compute barycentric
    weights vectorized over that box, keep the pixels inside the triangle, and
    z-test with PERSPECTIVE-CORRECT depth (interpolate 1/z, which is linear in
    screen space) — nearer pixels (larger 1/z) win;
  - optional SCENE OCCLUSION: a per-pixel scene inverse-depth lets foreground
    objects in the photo correctly cover the part (a part pixel is only drawn when
    it is nearer than the scene at that pixel);
  - everything is rendered at `supersample`× resolution and box-downsampled for
    cheap anti-aliasing;
  - an optional silhouette edge line is drawn for the product-shot look.
"""

from __future__ import annotations

import numpy as np

# Default key light direction (unit), pointing INTO the scene from upper-left-
# front, matching the camera frame (+X right, +Y down, +Z into scene).
DEFAULT_LIGHT = np.array([-0.35, -0.45, 0.82])


def project(
    verts_cam: np.ndarray, fx: float, fy: float, cx: float, cy: float
) -> np.ndarray:
    """Pinhole-project camera-space points (N,3), +Z into the scene, to pixels
    (N,2). Depth clamped positive so a point on the image plane can't divide by 0
    (behind-camera faces are culled separately by the caller)."""
    z = np.clip(verts_cam[:, 2], 1e-9, None)
    u = fx * verts_cam[:, 0] / z + cx
    v = fy * verts_cam[:, 1] / z + cy
    return np.stack([u, v], axis=1)


def face_shades(
    verts_cam: np.ndarray,
    faces: np.ndarray,
    light_dir: np.ndarray = DEFAULT_LIGHT,
    ambient: float = 0.42,
    diffuse: float = 0.5,
    rim: float = 0.14,
) -> np.ndarray:
    """A 0..1 brightness per face — flat shading with normals oriented toward the
    camera and lit from `light_dir`, plus a small left/rim term so the form reads.
    Pure numpy, no lighting-model dependency."""
    tris = verts_cam[faces]  # (F,3,3)
    n = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    nl = np.linalg.norm(n, axis=1, keepdims=True)
    nl[nl == 0] = 1.0
    n = n / nl
    centers = tris.mean(axis=1)
    # Flip normals that point away from the camera (origin at 0), so all face it.
    away = np.sum(n * centers, axis=1) > 0
    n[away] = -n[away]
    ldir = light_dir / (np.linalg.norm(light_dir) or 1.0)
    lit = np.clip(n @ (-ldir), 0.0, 1.0)  # -ldir: surfaces facing the light
    rim_term = np.clip(-n[:, 0], 0.0, 1.0)
    return np.clip(ambient + diffuse * lit + rim * rim_term, 0.0, 1.0)


def _rasterize(
    tris_xy: np.ndarray,
    tris_invz: np.ndarray,
    face_rgb: np.ndarray,
    W: int,
    H: int,
    scene_invz: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Core z-buffer fill. tris_xy (F,3,2) pixel coords; tris_invz (F,3) = 1/z per
    vertex; face_rgb (F,3) 0..255. Returns (color HxWx3 float, alpha HxW float 0/1,
    invz HxW). `scene_invz` (HxW) occludes: a part pixel is kept only where it is
    nearer than the scene (part_invz >= scene_invz)."""
    color = np.zeros((H, W, 3), dtype=np.float32)
    alpha = np.zeros((H, W), dtype=np.float32)
    zbuf = np.full((H, W), -np.inf, dtype=np.float64)  # store invz; larger = nearer

    for fi in range(tris_xy.shape[0]):
        (x0, y0), (x1, y1), (x2, y2) = tris_xy[fi]
        minx = max(int(np.floor(min(x0, x1, x2))), 0)
        maxx = min(int(np.ceil(max(x0, x1, x2))), W - 1)
        miny = max(int(np.floor(min(y0, y1, y2))), 0)
        maxy = min(int(np.ceil(max(y0, y1, y2))), H - 1)
        if minx > maxx or miny > maxy:
            continue
        denom = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        if abs(denom) < 1e-9:
            continue  # degenerate/edge-on triangle

        xs = np.arange(minx, maxx + 1) + 0.5
        ys = np.arange(miny, maxy + 1) + 0.5
        px, py = np.meshgrid(xs, ys)
        w0 = ((y1 - y2) * (px - x2) + (x2 - x1) * (py - y2)) / denom
        w1 = ((y2 - y0) * (px - x2) + (x0 - x2) * (py - y2)) / denom
        w2 = 1.0 - w0 - w1
        inside = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
        if not inside.any():
            continue

        iz0, iz1, iz2 = tris_invz[fi]
        invz = w0 * iz0 + w1 * iz1 + w2 * iz2

        sub_z = zbuf[miny : maxy + 1, minx : maxx + 1]
        better = inside & (invz > sub_z)
        if scene_invz is not None:
            sub_scene = scene_invz[miny : maxy + 1, minx : maxx + 1]
            better &= invz >= (sub_scene - 1e-9)  # keep only where nearer than scene
        if not better.any():
            continue

        sub_z[better] = invz[better]
        color[miny : maxy + 1, minx : maxx + 1][better] = face_rgb[fi]
        alpha[miny : maxy + 1, minx : maxx + 1][better] = 1.0

    return color, alpha, zbuf


def _box_downsample(arr: np.ndarray, s: int) -> np.ndarray:
    """Average s×s blocks (arr's first two dims must be exact multiples of s)."""
    if s == 1:
        return arr
    H, W = arr.shape[:2]
    if arr.ndim == 2:
        return arr.reshape(H // s, s, W // s, s).mean(axis=(1, 3))
    c = arr.shape[2]
    return arr.reshape(H // s, s, W // s, s, c).mean(axis=(1, 3))


def _silhouette_edge(alpha: np.ndarray) -> np.ndarray:
    """Boolean mask of part pixels on the silhouette boundary (a part pixel with a
    4-neighbour that is background). Pure numpy, no scipy."""
    solid = alpha > 0.5
    interior = np.ones_like(solid)
    interior[:-1, :] &= solid[1:, :]
    interior[1:, :] &= solid[:-1, :]
    interior[:, :-1] &= solid[:, 1:]
    interior[:, 1:] &= solid[:, :-1]
    return solid & ~interior


def render_mesh(
    verts_cam: np.ndarray,
    faces: np.ndarray,
    face_rgb: np.ndarray,
    fx: float,
    fy: float,
    cx: float,
    cy: float,
    W: int,
    H: int,
    *,
    supersample: int = 2,
    scene_depth: np.ndarray | None = None,
    edge_rgb: tuple[int, int, int] | None = None,
    edge_alpha: float = 1.0,
) -> np.ndarray:
    """Render a mesh to an RGBA uint8 layer (H,W,4) with a per-pixel z-buffer.

    `face_rgb` (F,3, 0..255) is the ALREADY-SHADED colour of each face (the caller
    owns shading + base colour). Faces with any vertex at/behind the camera are
    culled. `scene_depth` (HxW, SAME units as verts_cam's Z) enables scene
    occlusion. `edge_rgb` draws a silhouette line. Rendered at `supersample`× and
    box-downsampled for anti-aliasing."""
    s = max(1, int(supersample))
    Ws, Hs = W * s, H * s
    v2d = project(verts_cam, fx * s, fy * s, cx * s, cy * s)

    fz = verts_cam[:, 2][faces]  # (F,3) camera depth per face vertex
    keep = np.all(fz > 1e-6, axis=1)
    tris_xy = v2d[faces][keep]
    tris_invz = 1.0 / np.clip(fz[keep], 1e-9, None)
    frgb = np.asarray(face_rgb, dtype=np.float32)[keep]

    scene_invz = None
    if scene_depth is not None:
        sd = _resize_depth(scene_depth, Hs, Ws)
        scene_invz = 1.0 / np.clip(sd, 1e-9, None)

    color, alpha, _ = _rasterize(tris_xy, tris_invz, frgb, Ws, Hs, scene_invz)

    rgb = _box_downsample(color, s)
    a = _box_downsample(alpha, s)
    out = np.zeros((H, W, 4), dtype=np.float32)
    out[..., :3] = rgb
    out[..., 3] = a * 255.0

    if edge_rgb is not None:
        edge = _silhouette_edge(a)  # a = downsampled alpha, final resolution
        ec = np.array(edge_rgb, dtype=np.float32)
        out[edge, :3] = ec
        out[edge, 3] = np.maximum(out[edge, 3], 255.0 * float(edge_alpha))

    return np.clip(out, 0, 255).astype(np.uint8)


def _resize_depth(depth: np.ndarray, H: int, W: int) -> np.ndarray:
    """Nearest-neighbour resize a depth map to (H,W) with pure numpy (no PIL, so a
    NaN/inf-carrying float depth map isn't mangled by an image codec)."""
    depth = np.asarray(depth, dtype=np.float64)
    dh, dw = depth.shape[:2]
    if (dh, dw) == (H, W):
        return depth
    ys = (np.linspace(0, dh - 1, H)).round().astype(int)
    xs = (np.linspace(0, dw - 1, W)).round().astype(int)
    return depth[np.ix_(ys, xs)]

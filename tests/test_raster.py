"""Tests for api/raster.py — the shared software z-buffer rasterizer (M10b).

The headline correctness property is OCCLUSION: with a real per-pixel z-buffer
(unlike the old painter's algorithm) a nearer surface covers a farther one, for
both self-occluding parts and assemblies. The regression fixture is two
interlocking boxes whose correct occlusion is asserted at known pixels.
"""

from __future__ import annotations

import numpy as np
import trimesh

from api import raster


def _two_interlocking_boxes():
    """Box A (red) to the left and FARTHER; box B (blue) to the right and NEARER.
    They overlap in the image, so the z-buffer must draw B over A where they meet."""
    A = trimesh.creation.box(extents=(60, 60, 60))
    A.apply_translation([-15, 0, 260])
    B = trimesh.creation.box(extents=(60, 60, 60))
    B.apply_translation([15, 0, 220])  # nearer (smaller camera Z)
    verts = np.vstack([A.vertices, B.vertices])
    faces = np.vstack([A.faces, B.faces + len(A.vertices)])
    face_rgb = np.vstack(
        [
            np.tile([220, 40, 40], (len(A.faces), 1)),  # A red
            np.tile([40, 80, 220], (len(B.faces), 1)),  # B blue
        ]
    ).astype(float)
    return verts, faces, face_rgb


def test_z_buffer_occludes_correctly():
    verts, faces, face_rgb = _two_interlocking_boxes()
    W, H = 200, 160
    fx = fy = 300.0
    rgba = raster.render_mesh(verts, faces, face_rgb, fx, fy, W / 2, H / 2, W, H)
    img = rgba[..., :3].astype(int)
    alpha = rgba[..., 3]

    ys, xs = np.where(alpha > 128)
    assert xs.size > 0
    left = img[int(H / 2), xs.min() + 4]  # A-only region
    center = img[int(H / 2), int(W / 2)]  # overlap: B is nearer

    assert left[0] > left[2], f"left should be red (box A) {left}"
    assert (
        center[2] > center[0]
    ), f"center should be blue (nearer box B occludes A) {center}"


def test_painters_order_would_be_wrong():
    """Sanity: box B is authored FIRST in draw order yet still wins where nearer —
    i.e. correctness comes from the z-test, not from triangle submission order."""
    verts, faces, face_rgb = _two_interlocking_boxes()
    # Reverse face order so the FARTHER box's faces are drawn last (painter's
    # algorithm would then wrongly paint the far box over the near one).
    faces = faces[::-1].copy()
    face_rgb = face_rgb[::-1].copy()
    W, H = 200, 160
    rgba = raster.render_mesh(verts, faces, face_rgb, 300, 300, W / 2, H / 2, W, H)
    center = rgba[int(H / 2), int(W / 2), :3].astype(int)
    assert center[2] > center[0], "z-buffer must be order-independent"


def test_supersample_antialiases_edges():
    """Downsampled render has soft (fractional) alpha at the silhouette — evidence
    the 2× supersample is anti-aliasing. Uses a ROTATED box so its silhouette is
    diagonal (an axis-aligned edge snaps to the grid and wouldn't blend)."""
    box = trimesh.creation.box(extents=(80, 80, 80))
    box.apply_transform(trimesh.transformations.rotation_matrix(0.5, [0, 0, 1]))
    box.apply_transform(trimesh.transformations.rotation_matrix(0.4, [1, 0, 0]))
    box.apply_translation([0, 0, 240])
    verts = np.asarray(box.vertices)
    faces = np.asarray(box.faces)
    face_rgb = np.tile([200, 120, 40], (len(faces), 1)).astype(float)
    rgba = raster.render_mesh(
        verts, faces, face_rgb, 300, 300, 100, 80, 200, 160, supersample=2
    )
    a = rgba[..., 3]
    fractional = (a > 0) & (a < 255)
    assert fractional.sum() > 0, "expected anti-aliased (fractional-alpha) edge pixels"


def test_silhouette_edge_drawn():
    box = trimesh.creation.box(extents=(80, 80, 80))
    box.apply_translation([0, 0, 240])
    verts = np.asarray(box.vertices)
    faces = np.asarray(box.faces)
    face_rgb = np.tile([200, 120, 40], (len(faces), 1)).astype(float)
    plain = raster.render_mesh(verts, faces, face_rgb, 300, 300, 100, 80, 200, 160)
    edged = raster.render_mesh(
        verts, faces, face_rgb, 300, 300, 100, 80, 200, 160, edge_rgb=(10, 10, 10)
    )
    # The edged render has dark boundary pixels the plain one doesn't.
    dark_plain = ((plain[..., :3] < 40).all(axis=2) & (plain[..., 3] > 128)).sum()
    dark_edged = ((edged[..., :3] < 40).all(axis=2) & (edged[..., 3] > 128)).sum()
    assert dark_edged > dark_plain

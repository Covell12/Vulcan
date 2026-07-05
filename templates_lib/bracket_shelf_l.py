"""Parametric L-shaped shelf bracket (CadQuery). Track A template — see CLAUDE.md.

Geometry: an "L" profile (equal-length wall arm and shelf arm, joined at a right
angle) extruded along a fixed depth. The wall arm gets a row of clearance holes
for wood screws; the inner corner gets triangular gussets for stiffness under
load. `build_bracket` is a pure function: BracketShelfLParams in, cq.Workplane out.
"""

from __future__ import annotations

from typing import Literal

import cadquery as cq
from pydantic import BaseModel, Field, model_validator

from templates_lib.constants import MIN_WALL_MM
from templates_lib.registry import DimCallout, TemplateSpec, register_template

TEMPLATE_ID = "bracket_shelf_l"

# Clearance-hole diameters for common wood screw sizes (mm).
SCREW_CLEARANCE_MM = {
    "#6": 3.5,
    "#8": 4.2,
    "#10": 4.8,
}

# Number of corner gussets by expected shelf load.
RIB_COUNT_BY_LOAD = {
    "light": 1,
    "medium": 2,
    "heavy": 3,
}


def _screw_hole_y_positions(
    span_mm: float, thickness_mm: float, screw_count: int, clearance_mm: float
) -> list[float]:
    """Evenly space screw_count holes along the wall arm, clear of the corner and top edge.

    `margin` is measured from hole *center* to the arm's y=span_mm outer edge, so it
    must cover the hole radius plus MIN_WALL_MM of remaining material (the corner-side
    margin reuses the same value — not itself a min-wall requirement, just symmetry and
    clearance from the rib gussets). `min_spacing` is likewise center-to-center, so it
    must cover one full hole diameter plus MIN_WALL_MM between adjacent hole edges.
    """
    radius = clearance_mm / 2
    margin = radius + MIN_WALL_MM
    y_min = thickness_mm + margin
    y_max = span_mm - margin
    usable = y_max - y_min
    if usable <= 0:
        raise ValueError(
            f"span_mm ({span_mm}) is too short for screw holes with thickness_mm "
            f"({thickness_mm}) and the required clearance ({clearance_mm}mm); "
            "increase span_mm or reduce thickness_mm."
        )
    if screw_count == 1:
        return [y_min + usable / 2]
    spacing = usable / (screw_count - 1)
    min_spacing = clearance_mm + MIN_WALL_MM
    if spacing < min_spacing:
        raise ValueError(
            f"screw_count ({screw_count}) does not fit along the wall arm "
            f"(usable length {usable:.1f}mm gives {spacing:.1f}mm spacing, need "
            f"at least {min_spacing:.1f}mm so {MIN_WALL_MM}mm of material remains "
            "between adjacent holes); reduce screw_count or increase span_mm."
        )
    return [y_min + i * spacing for i in range(screw_count)]


class BracketShelfLParams(BaseModel):
    """Validated parameters for bracket_shelf_l. All lengths in millimeters."""

    # `ge`/`le` are the HARD buildable limits; `recommended_min/max` (in
    # json_schema_extra) are the softer typical range the UI shows and lets the
    # user expand past. Relational rules (below) + DFM are the real gate.
    span_mm: float = Field(
        default=120,
        ge=20,
        le=450,
        description="Length of each L leg (wall arm and shelf arm).",
        json_schema_extra={"recommended_min": 40, "recommended_max": 300},
    )
    depth_mm: float = Field(
        default=40,
        ge=8,
        le=300,
        description="Width of the bracket along the shelf edge (extrusion depth).",
        json_schema_extra={"recommended_min": 15, "recommended_max": 150},
    )
    thickness_mm: float = Field(
        default=4,
        ge=MIN_WALL_MM,
        le=20,
        description="Wall thickness of the L profile.",
        json_schema_extra={
            "recommended_min": MIN_WALL_MM,
            "recommended_max": 12,
            "hard_reason": f"can't go below the {MIN_WALL_MM} mm minimum printable wall.",
        },
    )
    screw_size: Literal["#6", "#8", "#10"] = Field(
        default="#8", description="Wood screw size for wall-mounting holes."
    )
    screw_count: int = Field(
        default=3,
        ge=2,
        le=12,
        description="Number of mounting holes in the wall arm.",
        json_schema_extra={
            "recommended_min": 2,
            "recommended_max": 6,
            "hard_reason": "need at least 2 holes for a stable wall mount.",
        },
    )
    load_hint: Literal["light", "medium", "heavy"] = Field(
        default="medium",
        description="Expected shelf load; determines the number of corner gussets.",
    )

    @model_validator(mode="after")
    def _check_geometry_fits(self) -> "BracketShelfLParams":
        if self.thickness_mm * 4 > self.span_mm:
            raise ValueError(
                f"thickness_mm ({self.thickness_mm}) is too large relative to span_mm "
                f"({self.span_mm}); span_mm must be at least 4x thickness_mm so the "
                "wall arm extends past the corner with room for screw holes."
            )
        clearance = SCREW_CLEARANCE_MM[self.screw_size]
        # Hole sits centered in depth; need MIN_WALL_MM of material in front of and
        # behind it (between the hole edge and the bracket's front/back faces).
        required_depth = clearance + 2 * MIN_WALL_MM
        if self.depth_mm < required_depth:
            raise ValueError(
                f"depth_mm ({self.depth_mm}) is too small for {self.screw_size} screw "
                f"holes (need at least {required_depth:.1f}mm so {MIN_WALL_MM}mm of "
                "material remains in front of and behind each hole)."
            )
        # Raises ValueError if screw_count doesn't fit — reuse the same logic build_bracket uses.
        _screw_hole_y_positions(
            self.span_mm, self.thickness_mm, self.screw_count, clearance
        )
        return self


def _add_ribs(solid: cq.Workplane, params: BracketShelfLParams) -> cq.Workplane:
    rib_count = RIB_COUNT_BY_LOAD[params.load_hint]
    span, depth, t = params.span_mm, params.depth_mm, params.thickness_mm

    rib_leg = 0.5 * (span - t)
    tri_pts = [(t, t), (t + rib_leg, t), (t, t + rib_leg)]

    slot = depth / rib_count
    fin_width = min(t, slot * 0.6)
    for i in range(rib_count):
        z_center = slot * (i + 0.5)
        rib = (
            cq.Workplane("XY")
            .workplane(offset=z_center - fin_width / 2)
            .polyline(tri_pts)
            .close()
            .extrude(fin_width)
        )
        solid = solid.union(rib)
    return solid


def _add_screw_holes(solid: cq.Workplane, params: BracketShelfLParams) -> cq.Workplane:
    clearance = SCREW_CLEARANCE_MM[params.screw_size]
    radius = clearance / 2
    z_center = params.depth_mm / 2
    y_positions = _screw_hole_y_positions(
        params.span_mm, params.thickness_mm, params.screw_count, clearance
    )
    for y in y_positions:
        hole = (
            cq.Workplane("YZ")
            .center(y, z_center)
            .circle(radius)
            .extrude(params.thickness_mm * 3, both=True)
        )
        solid = solid.cut(hole)
    return solid


def build_bracket(params: BracketShelfLParams) -> cq.Workplane:
    """Pure function: params -> CadQuery solid. No I/O, no globals, no randomness."""
    span, t = params.span_mm, params.thickness_mm

    profile_pts = [(0, 0), (span, 0), (span, t), (t, t), (t, span), (0, span)]
    solid = cq.Workplane("XY").polyline(profile_pts).close().extrude(params.depth_mm)

    solid = _add_ribs(solid, params)
    solid = _add_screw_holes(solid, params)
    return solid


def bracket_callouts(params: BracketShelfLParams) -> list[DimCallout]:
    """Dimension arrows for the preview: span along the shelf arm's bottom
    front edge, depth along the extrusion at the arm tip."""
    span, depth = params.span_mm, params.depth_mm
    return [
        DimCallout("span_mm", (0.0, 0.0, 0.0), (span, 0.0, 0.0), "span"),
        DimCallout("depth_mm", (span, 0.0, 0.0), (span, 0.0, depth), "depth"),
    ]


register_template(
    TemplateSpec(
        template_id=TEMPLATE_ID,
        label="L-shaped shelf bracket",
        params_model=BracketShelfLParams,
        build_fn=build_bracket,
        min_wall_violation={"thickness_mm": MIN_WALL_MM - 0.1},
        category="bracket",
        critical_dims=("span_mm", "depth_mm"),
        callouts_fn=bracket_callouts,
    )
)

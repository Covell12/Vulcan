"""Parametric replacement appliance knob (CadQuery). Track A template — see
CLAUDE.md and docs/vulcan-product-spec.pdf Appendix A ("knob.appliance").

Geometry: a cylindrical knob body with a bore cut into its bottom face to
receive the appliance's control shaft, optional exterior grip ribs, and an
optional top-face pointer fin (a rotational position indicator for a dial).

Appendix A only sketches parameter *names* (shaft type D/round/spline, shaft
dia, depth, knob dia, grip style, pointer?) with no numeric ranges or bore
clearance value — the ranges/defaults/flat-depth/rib and pointer geometry
below are our own engineering judgment, not sourced from the spec. The one
number the spec's Appendix A doesn't give but this milestone's instructions
do is the bore clearance: +0.2mm over nominal shaft_dia mm (a printed-fit
placeholder; expect this to be recalibrated once real prints/outcomes exist).

LIMITATION (v0): a real spline shaft has curved-flank teeth. We approximate
it here as a regular polygon bore with `spline_count` sides — close enough to
resist rotation for a first cut, but not a dimensionally-accurate spline fit.
"""

from __future__ import annotations

from typing import Literal

import cadquery as cq
from pydantic import BaseModel, Field, model_validator

from templates_lib.constants import MIN_WALL_MM
from templates_lib.registry import TemplateSpec, register_template

TEMPLATE_ID = "knob_appliance"

# Printed-fit clearance over nominal shaft_dia_mm, per this milestone's spec.
# Diametral (i.e. bore diameter = shaft_dia_mm + this), not per-side.
BORE_CLEARANCE_MM = 0.2

# D-shaft flat: the chord plane sits this fraction of the bore radius out from
# center, so the flat removes a modest cap rather than nearly bisecting the
# bore. Our own judgment — not sourced from the spec.
D_FLAT_RATIO = 0.85

# Ribbed grip: evenly-spaced vertical ridges around the knob's exterior.
RIB_COUNT = 16
RIB_WIDTH_MM = 2.0
RIB_PROTRUSION_MM = 1.0
RIB_OVERLAP_MM = (
    0.5  # ribs are welded into the body by this much so union doesn't leave a seam
)

# Pointer fin: a raised radial indicator on the top face.
POINTER_INNER_RATIO = 0.15  # fraction of knob radius where the fin starts
POINTER_OUTER_RATIO = (
    0.48  # fraction of knob radius where the fin ends (stays inboard of the edge)
)
POINTER_WIDTH_MM = 2.5
POINTER_HEIGHT_MM = 1.5


class KnobApplianceParams(BaseModel):
    """Validated parameters for knob_appliance. All lengths in millimeters."""

    shaft_type: Literal["round", "D", "spline"] = Field(
        default="round",
        description="Bore shape: plain round, round-with-flat (D), or polygon-approximated spline.",
    )
    shaft_dia_mm: float = Field(
        default=6, ge=3, le=15, description="Nominal control shaft diameter."
    )
    shaft_depth_mm: float = Field(
        default=12, ge=5, le=30, description="How far the bore extends into the knob."
    )
    knob_dia_mm: float = Field(
        default=32, ge=15, le=60, description="Outer diameter of the knob."
    )
    knob_height_mm: float = Field(
        default=22, ge=10, le=40, description="Overall height of the knob."
    )
    grip_style: Literal["ribbed", "smooth"] = Field(
        default="ribbed",
        description="Exterior finish: ribbed for grip, or a plain smooth cylinder.",
    )
    pointer: bool = Field(
        default=True,
        description="Add a raised radial fin on top as a dial-position indicator.",
    )
    spline_count: int = Field(
        default=6,
        ge=4,
        le=20,
        description="Number of sides in the polygon bore. Only used when shaft_type='spline'.",
    )

    @property
    def bore_dia_mm(self) -> float:
        return self.shaft_dia_mm + BORE_CLEARANCE_MM

    @model_validator(mode="after")
    def _check_walls(self) -> "KnobApplianceParams":
        radial_wall = (self.knob_dia_mm - self.bore_dia_mm) / 2
        if radial_wall < MIN_WALL_MM:
            raise ValueError(
                f"radial wall ({radial_wall:.2f}mm, from knob_dia_mm={self.knob_dia_mm} and "
                f"shaft_dia_mm={self.shaft_dia_mm}+{BORE_CLEARANCE_MM}mm clearance) is below "
                f"MIN_WALL_MM ({MIN_WALL_MM}mm); increase knob_dia_mm or decrease shaft_dia_mm."
            )
        cap_wall = self.knob_height_mm - self.shaft_depth_mm
        if cap_wall < MIN_WALL_MM:
            raise ValueError(
                f"top cap wall ({cap_wall:.2f}mm, from knob_height_mm={self.knob_height_mm} minus "
                f"shaft_depth_mm={self.shaft_depth_mm}) is below MIN_WALL_MM ({MIN_WALL_MM}mm); "
                "increase knob_height_mm or decrease shaft_depth_mm."
            )
        return self


def _bore_solid(params: KnobApplianceParams) -> cq.Workplane:
    bore_r = params.bore_dia_mm / 2
    depth = params.shaft_depth_mm

    if params.shaft_type == "round":
        return cq.Workplane("XY").circle(bore_r).extrude(depth)

    if params.shaft_type == "spline":
        return (
            cq.Workplane("XY")
            .polygon(params.spline_count, params.bore_dia_mm)
            .extrude(depth)
        )

    # "D": round bore with a flat chord cut on one side.
    round_bore = cq.Workplane("XY").circle(bore_r).extrude(depth)
    flat_x = bore_r * D_FLAT_RATIO
    span = bore_r * 2 + 2
    flat_cut = (
        cq.Workplane("XY")
        .box(span, span, depth + 2, centered=(True, True, False))
        .translate((flat_x + span / 2, 0, -1))
    )
    return round_bore.cut(flat_cut)


def _add_ribs(knob: cq.Workplane, params: KnobApplianceParams) -> cq.Workplane:
    knob_r = params.knob_dia_mm / 2
    rib = (
        cq.Workplane("XY")
        .box(
            RIB_OVERLAP_MM + RIB_PROTRUSION_MM,
            RIB_WIDTH_MM,
            params.knob_height_mm,
            centered=(False, True, False),
        )
        .translate((knob_r - RIB_OVERLAP_MM, 0, 0))
    )
    for i in range(RIB_COUNT):
        angle = 360.0 / RIB_COUNT * i
        knob = knob.union(rib.rotate((0, 0, 0), (0, 0, 1), angle))
    return knob


def _add_pointer(knob: cq.Workplane, params: KnobApplianceParams) -> cq.Workplane:
    knob_r = params.knob_dia_mm / 2
    inner = knob_r * POINTER_INNER_RATIO
    outer = knob_r * POINTER_OUTER_RATIO
    fin = (
        cq.Workplane("XY")
        .workplane(offset=params.knob_height_mm)
        .box(
            outer - inner,
            POINTER_WIDTH_MM,
            POINTER_HEIGHT_MM,
            centered=(False, True, False),
        )
        .translate((inner, 0, 0))
    )
    return knob.union(fin)


def build_knob(params: KnobApplianceParams) -> cq.Workplane:
    """Pure function: params -> CadQuery solid. No I/O, no globals, no randomness."""
    knob = (
        cq.Workplane("XY").circle(params.knob_dia_mm / 2).extrude(params.knob_height_mm)
    )
    knob = knob.cut(_bore_solid(params))

    if params.grip_style == "ribbed":
        knob = _add_ribs(knob, params)
    if params.pointer:
        knob = _add_pointer(knob, params)

    return knob


register_template(
    TemplateSpec(
        template_id=TEMPLATE_ID,
        label="Appliance knob",
        params_model=KnobApplianceParams,
        build_fn=build_knob,
        min_wall_violation={
            "knob_dia_mm": 15,
            "shaft_dia_mm": 14,
        },  # radial wall ~ 0.4mm
        category="knob",
        critical_dims=("shaft_dia_mm", "shaft_depth_mm"),
    )
)

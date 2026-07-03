"""Parametric tube/hose adapter (CadQuery). Track A template — see CLAUDE.md
and docs/vulcan-product-spec.pdf Appendix A ("adapter.tube").

Geometry: a concentric solid of revolution — build the outer (OD) silhouette
and the inner (bore) silhouette as two profiles sharing the same z-breakpoints,
revolve each 360 degrees into a solid, then cut the bore solid out of the
outer solid. Because both profiles share breakpoints, wall thickness varies
linearly (or not at all) between the two ends, so checking it only at the two
ends is sufficient — see `_check_wall_and_aspect_ratio`.

The bore is open at both flat end faces (nothing caps z=0 or z=total length),
so the part is hollow end-to-end and air/fluid can pass through, matching the
spec's "adapter" category. Appendix A only sketches parameter *names* (od_a,
id_a, od_b, id_b, engagement length, taper, thread?) with no numeric ranges,
defaults, or DFM values — the ranges/defaults/aspect-ratio and transition-
length rules below are our own engineering judgment, not sourced from the
spec. `thread?` from the spec is dropped for v0 (threads need their own DFM
pass); may return in a later milestone.
"""

from __future__ import annotations

import cadquery as cq
from pydantic import BaseModel, Field, model_validator

from templates_lib.constants import MIN_WALL_MM
from templates_lib.registry import DimCallout, TemplateSpec, register_template

TEMPLATE_ID = "adapter_tube"

# Shortest allowed conical transition between two differing ODs (mm), so a
# small diameter jump doesn't get an implausibly abrupt cone. Longer diameter
# jumps get a proportionally longer transition (see _transition_length_mm).
MIN_TRANSITION_MM = 6.0

# Keeps engagement length in a plausible range relative to that end's OD:
# long enough to actually grip a hose/tube, short enough not to be a fragile
# noodle. Our own judgment — the spec gives no numbers for this.
ENGAGEMENT_RATIO_MIN = 0.3
ENGAGEMENT_RATIO_MAX = 6.0

# v1 scope ceiling from the spec (p.2): parts bigger than this bounding box
# are explicitly out of scope.
MAX_PART_LENGTH_MM = 250.0


class AdapterTubeParams(BaseModel):
    """Validated parameters for adapter_tube. All lengths in millimeters."""

    od_a_mm: float = Field(
        default=20, ge=6, le=120, description="Outer diameter, end A."
    )
    id_a_mm: float = Field(
        default=14, ge=2, le=116, description="Inner (bore) diameter, end A."
    )
    od_b_mm: float = Field(
        default=30, ge=6, le=120, description="Outer diameter, end B."
    )
    id_b_mm: float = Field(
        default=22, ge=2, le=116, description="Inner (bore) diameter, end B."
    )
    engagement_a_mm: float = Field(
        default=15,
        ge=8,
        le=100,
        description="Length of the constant-diameter section at end A.",
    )
    engagement_b_mm: float = Field(
        default=20,
        ge=8,
        le=100,
        description="Length of the constant-diameter section at end B.",
    )
    taper: bool = Field(
        default=True,
        description="True: smooth conical transition between the two ends. False: an abrupt step/shoulder.",
    )

    @model_validator(mode="after")
    def _check_wall_and_aspect_ratio(self) -> "AdapterTubeParams":
        for end, od, id_ in (
            ("A", self.od_a_mm, self.id_a_mm),
            ("B", self.od_b_mm, self.id_b_mm),
        ):
            if id_ >= od:
                raise ValueError(
                    f"id_{end.lower()}_mm ({id_}) must be smaller than od_{end.lower()}_mm ({od})."
                )
            wall = (od - id_) / 2
            if wall < MIN_WALL_MM:
                raise ValueError(
                    f"end {end}: wall thickness ({wall:.2f}mm, from od={od}/id={id_}) is "
                    f"below MIN_WALL_MM ({MIN_WALL_MM}mm); increase od_{end.lower()}_mm or "
                    f"decrease id_{end.lower()}_mm."
                )

        for end, od, engagement in (
            ("A", self.od_a_mm, self.engagement_a_mm),
            ("B", self.od_b_mm, self.engagement_b_mm),
        ):
            ratio = engagement / od
            if not (ENGAGEMENT_RATIO_MIN <= ratio <= ENGAGEMENT_RATIO_MAX):
                raise ValueError(
                    f"engagement_{end.lower()}_mm ({engagement}) is an implausible length "
                    f"for od_{end.lower()}_mm ({od}) — ratio {ratio:.2f} is outside "
                    f"[{ENGAGEMENT_RATIO_MIN}, {ENGAGEMENT_RATIO_MAX}]."
                )

        if self.total_length_mm > MAX_PART_LENGTH_MM:
            raise ValueError(
                f"total length ({self.total_length_mm:.1f}mm) exceeds the v1 size ceiling "
                f"({MAX_PART_LENGTH_MM}mm); reduce engagement_a_mm/engagement_b_mm or the "
                "OD difference (which lengthens the taper transition)."
            )
        return self

    @property
    def transition_length_mm(self) -> float:
        if not self.taper:
            return 0.0
        return max(MIN_TRANSITION_MM, abs(self.od_a_mm - self.od_b_mm))

    @property
    def total_length_mm(self) -> float:
        return self.engagement_a_mm + self.transition_length_mm + self.engagement_b_mm


def _silhouette_points(
    r_a: float,
    r_b: float,
    engagement_a: float,
    engagement_b: float,
    taper: bool,
    transition_length: float,
) -> list[tuple[float, float]]:
    """Radius-vs-z profile for one surface (outer or inner), (0,0)-anchored on the axis
    at each end so revolving it produces flat end caps (open bore ends once cut)."""
    points = [(0.0, 0.0), (r_a, 0.0), (r_a, engagement_a)]
    if taper:
        points.append((r_b, engagement_a + transition_length))
    elif r_b != r_a:
        points.append((r_b, engagement_a))
    z_end = engagement_a + (transition_length if taper else 0.0) + engagement_b
    points.append((r_b, z_end))
    points.append((0.0, z_end))
    return points


def _revolve(points: list[tuple[float, float]]) -> cq.Workplane:
    # Profile is sketched in the XZ workplane (local x=radius, local y=height);
    # the revolve axis (0,0,0)->(0,1,0) is given in that same local frame, so it
    # maps to the global Z axis — the height axis our profile is drawn against.
    return (
        cq.Workplane("XZ").polyline(points).close().revolve(360, (0, 0, 0), (0, 1, 0))
    )


def build_adapter(params: AdapterTubeParams) -> cq.Workplane:
    """Pure function: params -> CadQuery solid. No I/O, no globals, no randomness."""
    transition = params.transition_length_mm

    outer_pts = _silhouette_points(
        params.od_a_mm / 2,
        params.od_b_mm / 2,
        params.engagement_a_mm,
        params.engagement_b_mm,
        params.taper,
        transition,
    )
    inner_pts = _silhouette_points(
        params.id_a_mm / 2,
        params.id_b_mm / 2,
        params.engagement_a_mm,
        params.engagement_b_mm,
        params.taper,
        transition,
    )

    outer = _revolve(outer_pts)
    inner = _revolve(inner_pts)
    return outer.cut(inner)


def adapter_callouts(params: AdapterTubeParams) -> list[DimCallout]:
    """Diameter arrows across each end face: outer + bore at end A (z=0) and
    end B (z=total length). The tube axis is global Z; diameters lie along X."""
    total = params.total_length_mm
    return [
        DimCallout(
            "od_a_mm",
            (-params.od_a_mm / 2, 0.0, 0.0),
            (params.od_a_mm / 2, 0.0, 0.0),
            "od A",
        ),
        DimCallout(
            "id_a_mm",
            (-params.id_a_mm / 2, 0.0, 0.0),
            (params.id_a_mm / 2, 0.0, 0.0),
            "bore A",
        ),
        DimCallout(
            "od_b_mm",
            (-params.od_b_mm / 2, 0.0, total),
            (params.od_b_mm / 2, 0.0, total),
            "od B",
        ),
        DimCallout(
            "id_b_mm",
            (-params.id_b_mm / 2, 0.0, total),
            (params.id_b_mm / 2, 0.0, total),
            "bore B",
        ),
    ]


register_template(
    TemplateSpec(
        template_id=TEMPLATE_ID,
        label="Tube/hose adapter",
        params_model=AdapterTubeParams,
        build_fn=build_adapter,
        min_wall_violation={
            "od_a_mm": 10,
            "id_a_mm": 9,
        },  # wall = 0.5mm, well below MIN_WALL_MM
        category="adapter",
        critical_dims=("od_a_mm", "id_a_mm", "od_b_mm", "id_b_mm"),
        callouts_fn=adapter_callouts,
    )
)

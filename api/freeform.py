"""Freeform generation orchestration (Track B): turn a request no registry
template fits into a dynamically-registered, sandbox-built ephemeral template.

The flow, all funnelled through the EXISTING machinery:
  1. Ask the model (api/codegen_provider) to author a parametric CadQuery module
     + parameter schema + critical dims.
  2. Statically verify the code (api/code_verifier), normalize the schema, build
     a pydantic params model from it (create_model).
  3. Test-build it in the sandbox (api/sandbox) with default params and check DFM
     (manifold + size ceiling).
  4. SELF-REPAIR: on any failure (unsafe code, sandbox error, timeout, DFM),
     retry generation up to 2 more times with the error appended. All attempts
     failing → log to the demand log and return failure.
  5. On success: persist code+schema+provenance under
     data/generated_templates/<id>/ and register an EphemeralTemplateSpec, so the
     normal intent flow (questions for critical dims → user_measured gate → the
     join → the runtime manifold gate) treats it like any other template.

Generated code is untrusted and is executed ONLY via api/sandbox. This module
never exec()s it.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, create_model

from api import codegen_provider
from api import exemplar_store
from api import sandbox
from api import vision_provider
from api.code_verifier import find_violations
from api.photo import PhotoInput
from templates_lib.registry import (
    EphemeralTemplateSpec,
    _ephemeral_build_placeholder,
    register_ephemeral_template,
    set_ephemeral_loader,
)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
GENERATED_DIR = DATA_DIR / "generated_templates"
DEMAND_LOG = DATA_DIR / "demand_log.jsonl"

# Freeform ids get a prefix so they're recognizable as generated (vs Track A).
EPHEMERAL_PREFIX = "gen_"

MAX_ATTEMPTS = 3  # generation ROUNDS: round 1 is best-of-N, rounds 2-3 are repairs
BEST_OF_N = 3  # candidates generated in parallel on the first round (competition)
CRITIQUE_THRESHOLD = 0.7  # below this visual-match score → regenerate with fixes
MAX_BBOX_MM = 250.0
_VALID_TYPES = {"number", "integer", "boolean", "choice"}

# Ranking weights for a shippable candidate (passed all hard gates). A shippable
# candidate always outscores a non-shippable one (see _score_candidate).
_W_GATES, _W_DIM, _W_CRIT = 0.5, 0.2, 0.3
# How far each failed candidate got, for ranking losers so the best feedback wins.
_STAGE_RANK = {
    "provider": 0,
    "validate": 1,
    "run": 2,
    "timeout": 2,
    "output": 2,
    "dfm": 3,
    "dimcontract": 4,
    "ok": 5,
}


# matplotlib/pyplot is process-global; best-of-N renders in worker threads, so
# serialize the render step to avoid corrupting pyplot's global figure registry.
_RENDER_LOCK = threading.Lock()


def _critique_enabled() -> bool:
    """Visual critique is real work (a vision call per candidate), so it's gated by
    VULCAN_CRITIQUE (default on). Tests default it off (see tests/conftest.py) for
    deterministic, offline, network-free runs."""
    return os.getenv("VULCAN_CRITIQUE", "on").strip().lower() in (
        "1",
        "on",
        "true",
        "yes",
    )


class GenerationRejected(ValueError):
    """A generated candidate we reject before/without a sandbox run (bad schema,
    unsafe code). Carries a message usable as self-repair feedback."""


@dataclass
class GenerationResult:
    ok: bool
    template_id: str | None = None
    spec: EphemeralTemplateSpec | None = None
    param_schema: list[dict[str, Any]] = field(default_factory=list)
    critical_dims: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    overlays: dict[str, dict[str, Any]] = field(default_factory=dict)
    dfm: dict[str, Any] | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    # M10a generation-quality provenance:
    critique: dict[str, Any] | None = None  # the winner's visual critique
    dim_contract: dict[str, Any] | None = (
        None  # the winner's dimensional-contract detail
    )
    score: float | None = None  # the winner's overall score
    candidates: list[dict[str, Any]] = field(
        default_factory=list
    )  # all evaluated (review page)


@dataclass
class Candidate:
    """One generated candidate, fully evaluated (gates + dimensional contract +
    visual critique). Holds no filesystem paths — renders/exports live only inside
    the temp dir during evaluation; what survives is the code, scores, and the
    critique dict, so a losing candidate is still fully reviewable."""

    code: str
    param_schema: list[dict[str, Any]]
    critical: list[str]
    assumptions: list[str]
    overlays: dict[str, dict[str, Any]]
    stage: str  # where it stopped: provider/validate/run/.../dfm/dimcontract/ok
    ok_hard: bool  # passed sandbox + DFM + dimensional contract (shippable geometry)
    error: str | None = None
    dfm: dict[str, Any] | None = None
    dim_score: float = 0.0
    dim_detail: dict[str, Any] | None = None
    critique: dict[str, Any] | None = None  # {matches_request, defects, targeted_fixes}
    feedback: str | None = None  # self-repair feedback if not ideal
    score: float = 0.0

    @property
    def critique_score(self) -> float | None:
        if not self.critique:
            return None
        try:
            return float(self.critique.get("matches_request"))
        except (TypeError, ValueError):
            return None

    @property
    def critique_ok(self) -> bool:
        """A skipped critique (None) does NOT block — only a real score below the
        threshold does."""
        cs = self.critique_score
        return cs is None or cs >= CRITIQUE_THRESHOLD

    def provenance(self, *, winner: bool) -> dict[str, Any]:
        return {
            "winner": winner,
            "stage": self.stage,
            "ok_hard": self.ok_hard,
            "score": round(self.score, 4),
            "dim_score": round(self.dim_score, 4),
            "critique": self.critique,
            "dfm": self.dfm,
            "error": self.error,
            "code": self.code,
            "param_schema": self.param_schema,
        }


# ---------------------------------------------------------------------------
# Param-schema normalization + pydantic model synthesis
# ---------------------------------------------------------------------------


def _coerce_default(value: Any, ftype: str, choices: list[str] | None) -> Any:
    try:
        if ftype == "integer":
            return int(round(float(value)))
        if ftype == "number":
            return float(value)
        if ftype == "boolean":
            if isinstance(value, str):
                return value.strip().lower() in ("true", "1", "yes", "on")
            return bool(value)
        # choice
        s = str(value)
        if choices and s not in choices:
            return choices[0]
        return s
    except (TypeError, ValueError):
        if ftype in ("number", "integer"):
            return 0
        if ftype == "boolean":
            return False
        return (choices or [""])[0]


def normalize_param_schema(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Coerce the model's param_schema into the canonical form_fields_for() shape
    (name/label/type/default/minimum/maximum/choices/description). Raises
    GenerationRejected on a structurally unusable schema."""
    if not isinstance(raw, list):
        raise GenerationRejected("param_schema must be a list of parameter fields")
    fields: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict) or not entry.get("name"):
            raise GenerationRejected("every param field needs a name")
        name = str(entry["name"])
        if name in seen:
            raise GenerationRejected(f"duplicate parameter name '{name}'")
        seen.add(name)
        ftype = entry.get("type", "number")
        if ftype not in _VALID_TYPES:
            ftype = "number"
        choices = entry.get("choices")
        if ftype == "choice" and not (isinstance(choices, list) and choices):
            raise GenerationRejected(
                f"choice param '{name}' needs a non-empty choices list"
            )
        if ftype != "choice":
            choices = None
        minimum = entry.get("minimum") if ftype in ("number", "integer") else None
        maximum = entry.get("maximum") if ftype in ("number", "integer") else None
        fields.append(
            {
                "name": name,
                "label": name.replace("_mm", " (mm)")
                .replace("_", " ")
                .strip()
                .capitalize(),
                "type": ftype,
                "default": _coerce_default(entry.get("default"), ftype, choices),
                "minimum": minimum,
                "maximum": maximum,
                "choices": choices,
                "description": str(entry.get("description", "")),
            }
        )
    return fields


def build_params_model(param_schema: list[dict[str, Any]]) -> type[BaseModel]:
    """Build a pydantic model from a normalized param schema, with ge/le bounds
    and Literal enums, so it validates exactly like a hand-written template's
    params model. Every field has a default, so Model() works with no args."""
    definitions: dict[str, tuple] = {}
    for f in param_schema:
        ftype, default = f["type"], f["default"]
        if ftype == "choice":
            pytype: Any = Literal[tuple(f["choices"])]
        elif ftype == "integer":
            pytype = int
        elif ftype == "boolean":
            pytype = bool
        else:
            pytype = float
        constraints: dict[str, Any] = {"description": f.get("description", "")}
        if ftype in ("number", "integer"):
            if f.get("minimum") is not None:
                constraints["ge"] = f["minimum"]
            if f.get("maximum") is not None:
                constraints["le"] = f["maximum"]
        definitions[f["name"]] = (pytype, Field(default=default, **constraints))
    return create_model("EphemeralParams", **definitions)


def default_params(param_schema: list[dict[str, Any]]) -> dict[str, Any]:
    return {f["name"]: f["default"] for f in param_schema}


# ---------------------------------------------------------------------------
# DFM check (manifold + size ceiling) on a sandbox-exported STL
# ---------------------------------------------------------------------------


def dfm_check(stl_path: Path) -> tuple[bool, dict[str, Any]]:
    """Automated DFM gate on the exported mesh: watertight/manifold and within
    the size ceiling. (Min-wall is NOT auto-verified — a reliable thickness
    analysis on an arbitrary mesh is out of scope for v0; the codegen prompt asks
    for it and founder review is the backstop. Stated in the security notes.)"""
    import trimesh

    from api.rendering import heal_mesh_file, mesh_body_count

    # Try a light, print-safe repair first (and re-export the healed STL), so a
    # valid generated solid whose tessellation has hairline gaps / bad winding
    # isn't wrongly rejected — a genuinely broken mesh still fails.
    manifold = heal_mesh_file(stl_path)
    mesh = trimesh.load(str(stl_path), force="mesh")
    faces = getattr(mesh, "faces", [])
    manifold = manifold and len(faces) > 0
    # Must also be ONE connected body — no floating/disjoint pieces. (Two disjoint
    # closed bodies are each watertight, so the manifold check alone misses an
    # add-on the model placed but never fused to the part.)
    bodies = mesh_body_count(stl_path)
    connected = bodies == 1
    bounds = getattr(mesh, "bounds", None)
    if bounds is None:
        extent = [0.0, 0.0, 0.0]
    else:
        extent = [float(bounds[1][i] - bounds[0][i]) for i in range(3)]
    max_extent = max(extent) if extent else 0.0
    within = 0 < max_extent <= MAX_BBOX_MM + 1e-6
    results = {
        "manifold": manifold,
        "connected": connected,
        "body_count": bodies,
        "within_size": within,
        "max_extent_mm": round(max_extent, 2),
        "bbox_mm": [round(x, 2) for x in extent],
        "size_ceiling_mm": MAX_BBOX_MM,
    }
    return (manifold and connected and within), results


def dfm_check_parts(parts: list[dict]) -> tuple[bool, dict[str, Any]]:
    """Run the DFM gate on EVERY part of an assembly (each must be its own single
    connected manifold solid within the size ceiling). Returns (all_ok, report)
    where the report aggregates the per-part results plus a `parts` list."""
    if not parts:
        return False, {"part_count": 0, "parts": []}
    per: list[dict[str, Any]] = []
    all_ok = True
    for p in parts:
        ok, d = dfm_check(p["stl"])
        all_ok = all_ok and ok
        per.append({"name": p.get("name", "part"), **d})
    agg: dict[str, Any] = {"part_count": len(parts), "parts": per}
    # Roll per-part flags up so existing single-part consumers (and the review
    # dashboard's one-line summary) still see meaningful top-level values.
    agg["manifold"] = all(p["manifold"] for p in per)
    agg["connected"] = all(p.get("connected", True) for p in per)
    agg["body_count"] = sum(p.get("body_count", 1) for p in per)
    agg["within_size"] = all(p["within_size"] for p in per)
    agg["max_extent_mm"] = max(p["max_extent_mm"] for p in per)
    agg["size_ceiling_mm"] = per[0]["size_ceiling_mm"]
    return all_ok, agg


def _dfm_feedback(dfm: dict[str, Any]) -> str:
    # Multi-part assembly: point the model at the specific parts that failed.
    if dfm.get("parts") and dfm.get("part_count", 1) > 1:
        problems = []
        for p in dfm["parts"]:
            issues = []
            if not p["manifold"]:
                issues.append("not watertight/manifold")
            if not p.get("connected", True):
                issues.append(f"{p.get('body_count', '?')} disconnected pieces")
            if not p["within_size"]:
                issues.append(f"too big ({p['max_extent_mm']}mm)")
            if issues:
                problems.append(f"part '{p['name']}': " + ", ".join(issues))
        base = "; ".join(problems) or "unknown DFM failure"
        return (
            base
            + ". Each part must be ONE connected, watertight solid within the size "
            "ceiling; keep the pieces as SEPARATE parts that fit together (don't fuse "
            "them into one), positioned where they mate."
        )
    problems = []
    if not dfm["manifold"]:
        problems.append(
            "the solid is not watertight/manifold (must be one closed body)"
        )
    if not dfm.get("connected", True):
        problems.append(
            f"the part has {dfm.get('body_count', '?')} DISCONNECTED pieces (floating "
            "parts) — everything must be UNIONed into ONE connected solid (fuse every "
            "add-on to the body; make touching parts actually overlap in volume)"
        )
    if not dfm["within_size"]:
        problems.append(
            f"the part's largest dimension is {dfm['max_extent_mm']}mm, over the "
            f"{dfm['size_ceiling_mm']}mm ceiling (shrink it or tighten param maximums)"
        )
    return "; ".join(problems) or "unknown DFM failure"


# ---------------------------------------------------------------------------
# Dimensional contract: every declared length param must actually move the solid
# ---------------------------------------------------------------------------
#
# The codegen model sometimes declares a parameter (so the user is asked to
# measure it) but writes build() to IGNORE it — the value has no effect on the
# geometry, so measuring it is theatre. We catch that by PERTURBING each length
# param and rebuilding in the sandbox: if changing the value by a real delta
# leaves the built solid's bounding box AND volume unchanged, the param is dead.
# Volume (not just bbox) is measured so an internal feature the bbox can't see —
# a hole diameter, a pocket depth — still counts as "reflected".

_MAX_PROBES = 6  # cap sensitivity rebuilds per candidate to bound sandbox time
_DIM_MIN_BBOX_MM = 0.1  # a bbox shift below this (and 0.5% of size) doesn't count
_DIM_REL = 0.005  # a >=0.5% bbox-or-volume change counts as "reflected"


def _is_length_param(f: dict[str, Any]) -> bool:
    """A fit dimension we can sensitivity-probe: a numeric millimetre length
    (mm-valued names end in _mm, per the codegen contract). Counts/booleans/
    choices/angles are not length params and aren't probed."""
    return f["type"] in ("number", "integer") and str(f["name"]).endswith("_mm")


def build_dimensional_probes(
    param_schema: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """One perturbed full-params set per length param (up to _MAX_PROBES), for the
    sandbox to rebuild and measure. Perturb by max(25%, 5mm); move up if it stays
    within `maximum`, else down within `minimum`, else skip (bounds too tight)."""
    base = default_params(param_schema)
    probes: list[dict[str, Any]] = []
    for f in param_schema:
        if not _is_length_param(f):
            continue
        name = f["name"]
        try:
            b = float(base[name])
        except (TypeError, ValueError):
            continue
        delta = max(0.25 * abs(b), 5.0)
        lo, hi = f.get("minimum"), f.get("maximum")
        up, down = b + delta, b - delta
        if hi is None or up <= hi:
            v: float = up
        elif lo is None or down >= lo:
            v = down
        else:
            continue  # can't move within bounds
        if f["type"] == "integer":
            v = round(v)
        if abs(v - b) < 1e-9:
            continue
        probes.append(
            {"name": name, "params": {**base, name: v}, "base": b, "value": v}
        )
        if len(probes) >= _MAX_PROBES:
            break
    return probes


def _dim_feedback(dead: list[str], probe_meta: dict[str, dict[str, Any]]) -> str:
    bits = []
    for name in dead:
        pm = probe_meta.get(name, {})
        b, v = pm.get("base"), pm.get("value")
        if b is not None and v is not None:
            bits.append(f"'{name}' (changed from {b:g} to {v:g} mm)")
        else:
            bits.append(f"'{name}'")
    return (
        "dimensional contract violation: parameter(s) "
        + ", ".join(bits)
        + " are declared but do NOT affect the built geometry — perturbing them left "
        "the solid's bounding box AND volume unchanged. Every length parameter must "
        "drive a real feature; rewrite build() to actually use "
        + ", ".join(f"params['{n}']" for n in dead)
        + "."
    )


def dimensional_contract_check(
    param_schema: list[dict[str, Any]], measurements: dict[str, Any] | None
) -> tuple[bool, float, str | None, dict[str, Any]]:
    """Given the sandbox's baseline + per-probe measurements, verify every length
    param moved the solid. Returns (ok, score 0-1, self-repair feedback or None,
    detail). ok=False (with a param-named feedback) iff a probed param changed
    NOTHING. Inconclusive params (couldn't perturb within bounds, or the perturbed
    build failed) are neither scored nor blocked — we only fail on hard evidence."""
    length_params = [f["name"] for f in param_schema if _is_length_param(f)]
    if not length_params:
        return True, 1.0, None, {"length_params": 0, "checked": [], "dead": []}

    meas = measurements or {}
    base = meas.get("baseline") or {}
    base_bbox = base.get("bbox")
    base_vol = base.get("volume")
    if not base_bbox:
        return True, 1.0, None, {"skipped": "no baseline measurement"}
    base_size = max(base_bbox) or 1.0

    probe_meas = meas.get("probes") or {}
    probe_meta = {p["name"]: p for p in build_dimensional_probes(param_schema)}
    checked: list[str] = []
    dead: list[str] = []
    details: list[dict[str, Any]] = []
    for name in length_params:
        pm = probe_meta.get(name)
        r = probe_meas.get(name)
        if pm is None or not r or not r.get("built"):
            continue  # inconclusive
        pbbox, pvol = r.get("bbox"), r.get("volume")
        bbox_change = (
            max(abs(pbbox[i] - base_bbox[i]) for i in range(3)) if pbbox else 0.0
        )
        vol_rel = (
            abs(pvol - base_vol) / max(abs(base_vol), 1e-6)
            if (pvol is not None and base_vol is not None)
            else 0.0
        )
        reflected = (
            bbox_change > max(_DIM_MIN_BBOX_MM, _DIM_REL * base_size)
            or vol_rel > _DIM_REL
        )
        checked.append(name)
        details.append(
            {
                "param": name,
                "delta_mm": round(abs(pm["value"] - pm["base"]), 3),
                "bbox_change_mm": round(bbox_change, 3),
                "volume_change_frac": round(vol_rel, 4),
                "reflected": reflected,
            }
        )
        if not reflected:
            dead.append(name)

    total = len(checked)
    score = 1.0 if total == 0 else (total - len(dead)) / total
    detail = {
        "length_params": len(length_params),
        "checked": checked,
        "dead": dead,
        "probes": details,
    }
    if dead:
        return False, score, _dim_feedback(dead, probe_meta), detail
    return True, score, None, detail


# ---------------------------------------------------------------------------
# Persistence + rehydration
# ---------------------------------------------------------------------------


def _template_dir(gen_id: str) -> Path:
    return GENERATED_DIR / gen_id


def _persist(
    gen_id: str,
    code: str,
    param_schema: list[dict[str, Any]],
    critical_dims: list[str],
    provenance: dict[str, Any],
) -> None:
    d = _template_dir(gen_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "code.py").write_text(code, encoding="utf-8")
    (d / "schema.json").write_text(
        json.dumps(
            {"param_schema": param_schema, "critical_dims": critical_dims}, indent=2
        ),
        encoding="utf-8",
    )
    (d / "provenance.json").write_text(
        json.dumps(provenance, indent=2), encoding="utf-8"
    )


def _make_spec(
    gen_id: str, code: str, param_schema: list[dict[str, Any]], critical_dims: list[str]
) -> EphemeralTemplateSpec:
    spec = EphemeralTemplateSpec(
        template_id=gen_id,
        label="Custom design",
        params_model=build_params_model(param_schema),
        build_fn=_ephemeral_build_placeholder,
        min_wall_violation={},
        category="other",
        critical_dims=tuple(critical_dims),
        callouts_fn=lambda params: [],
        code=code,
    )
    register_ephemeral_template(spec)
    return spec


def load_ephemeral_from_disk(gen_id: str) -> EphemeralTemplateSpec | None:
    """Rehydrate a stored freeform template after a restart. Called by
    registry.get_template on a miss. Returns None (never raises) if there's
    nothing to load or it's unreadable."""
    if not gen_id.startswith(EPHEMERAL_PREFIX):
        return None
    d = _template_dir(gen_id)
    try:
        code = (d / "code.py").read_text(encoding="utf-8")
        schema = json.loads((d / "schema.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return _make_spec(
            gen_id, code, schema["param_schema"], schema.get("critical_dims", [])
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Demand log
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_demand(
    request_text: str, reason: str, extra: dict[str, Any] | None = None
) -> None:
    """Append a request we couldn't serve automatically to the demand log — the
    corpus that tells the founder what to build/templatize next."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    entry = {"request": request_text, "reason": reason, "created_at": _now()}
    if extra:
        entry.update(extra)
    with open(DEMAND_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# The self-repair generation loop
# ---------------------------------------------------------------------------


def _validate_candidate(
    code: str, param_schema: list[dict[str, Any]], critical_dims: list[str]
) -> None:
    """Cheap checks before spending a sandbox run. Raises GenerationRejected with
    self-repair feedback."""
    violations = find_violations(code)
    if violations:
        raise GenerationRejected(
            "the code failed the safety verifier (imports/constructs must be limited "
            "to cadquery/math/numpy and a build(params) function):\n- "
            + "\n- ".join(violations)
        )
    names = {f["name"] for f in param_schema}
    missing = [c for c in critical_dims if c not in names]
    if missing:
        raise GenerationRejected(
            f"critical_dims {missing} are not present in param_schema names {sorted(names)}"
        )


def _normalize_overlays(raw: Any) -> dict[str, dict[str, Any]]:
    """Turn the model's overlays list into {dim_name: overlay}, dropping the
    dim_name key and any null fields so each overlay matches the intent's
    question-overlay shape (kind + only the geometry that kind uses)."""
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, list):
        return out
    for o in raw:
        if not isinstance(o, dict) or not o.get("dim_name") or not o.get("kind"):
            continue
        overlay = {k: v for k, v in o.items() if k != "dim_name" and v is not None}
        overlay.setdefault("photo_index", 0)
        out[o["dim_name"]] = overlay
    return out


def _probe_timeout(n_probes: int) -> int:
    """Give the sandbox a bit more wall-clock when it must also rebuild N probes."""
    return min(120, sandbox.DEFAULT_TIMEOUT_S + 8 * max(0, n_probes))


def _critique_feedback(critique: dict[str, Any]) -> str:
    """Turn a below-threshold visual critique into self-repair feedback."""
    defects = [str(d) for d in (critique.get("defects") or []) if d]
    fixes = [str(f) for f in (critique.get("targeted_fixes") or []) if f]
    lines = []
    if defects:
        lines.append(
            "A visual review of the rendered part found: " + "; ".join(defects) + "."
        )
    if fixes:
        lines.append("Apply these specific fixes: " + "; ".join(fixes) + ".")
    return " ".join(lines) or (
        "The rendered part did not clearly match the request; redesign it to be "
        "obviously the requested part with the right functional features."
    )


def _score_candidate(c: Candidate) -> float:
    """Overall ranking score. A shippable candidate (all hard gates passed) scores
    in [0.5, 1.0] = gates + dimensional-contract + critique; a non-shippable one
    scores in [0, 0.32] by how far it got — so shippable ALWAYS wins."""
    if c.ok_hard:
        crit = c.critique_score if c.critique_score is not None else CRITIQUE_THRESHOLD
        c.score = _W_GATES * 1.0 + _W_DIM * c.dim_score + _W_CRIT * crit
    else:
        c.score = 0.4 * (_STAGE_RANK.get(c.stage, 0) / 5.0)
    return c.score


def _run_critique(
    parts: list[dict],
    request_text: str,
    params: dict[str, Any],
    param_schema: list[dict[str, Any]],
    tmpdir: str,
) -> dict[str, Any] | None:
    """Render the built part from 4 canonical views and get a visual critique.
    Best-effort: any failure (provider down, render/pyplot error) → None, so
    critique never blocks an otherwise-good candidate. The render is serialized
    (matplotlib/pyplot is process-global and best-of-N runs candidates in threads)."""
    try:
        from api.rendering import render_canonical_views

        stl_paths = [p["stl"] for p in parts]
        with _RENDER_LOCK:
            views = render_canonical_views(stl_paths, Path(tmpdir) / "views")
        if not views:
            return None
        return vision_provider.critique_design(
            views, request_text, params, param_schema
        )
    except vision_provider.VisionProviderError:
        return None
    except Exception:  # noqa: BLE001 — critique is advisory; never let it crash a build
        return None


def _evaluate_candidate(
    gen: dict[str, Any],
    request_text: str,
    run_critique: bool,
    progress: Any = None,
) -> Candidate:
    """Take one raw codegen output through every gate: validate → sandbox build
    (+ dimensional probes) → DFM → dimensional contract → visual critique. Returns
    a fully-scored Candidate. Never raises."""
    code = gen.get("cadquery_code") or ""
    critical = list(gen.get("critical_dims") or [])
    assumptions = list(gen.get("assumptions") or [])
    overlays = _normalize_overlays(gen.get("overlays"))

    def make(
        stage: str, *, ok_hard: bool = False, param_schema=None, **kw
    ) -> Candidate:
        c = Candidate(
            code=code,
            param_schema=param_schema or [],
            critical=critical,
            assumptions=assumptions,
            overlays=overlays,
            stage=stage,
            ok_hard=ok_hard,
            **kw,
        )
        _score_candidate(c)
        return c

    try:
        param_schema = normalize_param_schema(gen.get("param_schema") or [])
        _validate_candidate(code, param_schema, critical)
    except GenerationRejected as e:
        return make("validate", error=str(e), feedback=str(e))

    probes = build_dimensional_probes(param_schema)
    with tempfile.TemporaryDirectory(prefix="vulcan_gen_") as tmp:
        result = sandbox.run_generated_build(
            code,
            default_params(param_schema),
            Path(tmp) / "out",
            timeout_s=_probe_timeout(len(probes)),
            probe_params=probes,
        )
        if not result.ok:
            fb = f"The generated code failed to build ({result.stage}): {result.error}"
            return make(
                result.stage, param_schema=param_schema, error=result.error, feedback=fb
            )

        dfm_ok, dfm = dfm_check_parts(result.parts or [])
        if not dfm_ok:
            fb = "The built part failed DFM checks: " + _dfm_feedback(dfm)
            return make("dfm", param_schema=param_schema, dfm=dfm, feedback=fb)

        dim_ok, dim_score, dim_fb, dim_detail = dimensional_contract_check(
            param_schema, result.measurements
        )
        if not dim_ok:
            return make(
                "dimcontract",
                param_schema=param_schema,
                dfm=dfm,
                dim_score=dim_score,
                dim_detail=dim_detail,
                error=dim_fb,
                feedback=dim_fb,
            )

        critique = None
        if run_critique:
            if progress:
                progress("critiquing")
            critique = _run_critique(
                result.parts or [],
                request_text,
                default_params(param_schema),
                param_schema,
                tmp,
            )

    c = make(
        "ok",
        ok_hard=True,
        param_schema=param_schema,
        dfm=dfm,
        dim_score=dim_score,
        dim_detail=dim_detail,
        critique=critique,
    )
    if critique is not None and not c.critique_ok:
        c.feedback = _critique_feedback(critique)
    return c


def _generate_one(
    request_text: str,
    photos: list[PhotoInput],
    dims_hints: list[dict[str, Any]] | None,
    feedback: str | None,
    exemplars: list[dict[str, Any]] | None,
    run_critique: bool,
    progress: Any = None,
) -> Candidate:
    """Generate ONE candidate (codegen → full evaluation). Runs in a worker thread
    during best-of-N; never raises."""
    try:
        gen = codegen_provider.generate_template(
            request_text,
            photos,
            dims_hints,
            retry_feedback=feedback,
            exemplars=exemplars,
        )
    except codegen_provider.CodegenProviderError as e:
        c = Candidate(
            code="",
            param_schema=[],
            critical=[],
            assumptions=[],
            overlays={},
            stage="provider",
            ok_hard=False,
            error=str(e),
            feedback=f"The generation service errored: {e}",
        )
        _score_candidate(c)
        return c
    return _evaluate_candidate(gen, request_text, run_critique, progress)


def _attempt_record(rnd: int, c: Candidate) -> dict[str, Any]:
    return {
        "round": rnd,
        "stage": c.stage,
        "score": round(c.score, 4),
        "ok_hard": c.ok_hard,
        "dim_score": round(c.dim_score, 4),
        "dfm": c.dfm,
        "critique": c.critique,
        "error": c.error,
    }


def _finalize(
    winner: Candidate,
    request_text: str,
    attempts: list[dict[str, Any]],
    all_candidates: list[Candidate],
    critiques: list[dict[str, Any]],
    *,
    low_critique: bool = False,
) -> GenerationResult:
    """Persist the winning candidate + full provenance (every evaluated candidate,
    all critiques) and register it as an ephemeral template."""
    gen_id = EPHEMERAL_PREFIX + uuid.uuid4().hex[:12]
    provenance_candidates = [
        c.provenance(winner=(c is winner))
        for c in all_candidates
        if c.code or c is winner
    ]
    _persist(
        gen_id,
        winner.code,
        winner.param_schema,
        winner.critical,
        {
            "request": request_text,
            "assumptions": winner.assumptions,
            "critical_dims": winner.critical,
            "overlays": winner.overlays,
            "dfm": winner.dfm,
            "attempts": attempts,
            "critique": winner.critique,
            "dim_contract": winner.dim_detail,
            "score": round(winner.score, 4),
            "best_of_n": BEST_OF_N,
            "low_critique": low_critique,
            "candidates": provenance_candidates,
            "critiques": critiques,
            "created_at": _now(),
        },
    )
    spec = _make_spec(gen_id, winner.code, winner.param_schema, winner.critical)
    return GenerationResult(
        ok=True,
        template_id=gen_id,
        spec=spec,
        param_schema=winner.param_schema,
        critical_dims=winner.critical,
        assumptions=winner.assumptions,
        overlays=winner.overlays,
        dfm=winner.dfm,
        attempts=attempts,
        critique=winner.critique,
        dim_contract=winner.dim_detail,
        score=round(winner.score, 4),
        candidates=provenance_candidates,
    )


def generate_and_register(
    request_text: str,
    photos: list[PhotoInput],
    dims_hints: list[dict[str, Any]] | None = None,
    *,
    progress: Any = None,
) -> GenerationResult:
    """The Track B loop with COMPETITION + EYES + MEMORY (M10a):

      round 1  → best-of-N: BEST_OF_N candidates generated IN PARALLEL, each taken
                 through validate → sandbox build (+ dimensional probes) → DFM →
                 dimensional contract → visual critique, then scored; the winner
                 is auto-selected.
      rounds   → if the winner isn't shippable OR its visual-match score is below
       2..MAX    CRITIQUE_THRESHOLD, regenerate ONE candidate with the winner's
                 feedback appended (critique defects/fixes, or the gate error).

    Retrieved approved exemplars (api/exemplar_store) are fed to codegen as extra
    few-shot references. Returns a GenerationResult; if the geometry is valid but
    critique never cleared the bar, the best shippable candidate still ships (the
    founder review gate is the final backstop). Total failure → demand log."""
    run_critique = _critique_enabled()
    if progress:
        progress("generating")
    try:
        exemplars = exemplar_store.retrieve(request_text, k=2) or None
    except Exception:  # noqa: BLE001 — memory is an optimization; never block on it
        exemplars = None

    attempts: list[dict[str, Any]] = []
    all_candidates: list[Candidate] = []
    critiques: list[dict[str, Any]] = []
    best_shippable: Candidate | None = None
    feedback: str | None = None

    for rnd in range(1, MAX_ATTEMPTS + 1):
        n = BEST_OF_N if rnd == 1 else 1
        if n == 1:
            candidates = [
                _generate_one(
                    request_text,
                    photos,
                    dims_hints,
                    feedback,
                    exemplars,
                    run_critique,
                    progress,
                )
            ]
        else:
            with ThreadPoolExecutor(max_workers=n) as pool:
                futures = [
                    pool.submit(
                        _generate_one,
                        request_text,
                        photos,
                        dims_hints,
                        feedback,
                        exemplars,
                        run_critique,
                        progress,
                    )
                    for _ in range(n)
                ]
                candidates = [f.result() for f in futures]  # submission order

        for c in candidates:
            attempts.append(_attempt_record(rnd, c))
            if c.critique is not None:
                critiques.append(
                    {
                        "round": rnd,
                        "matches_request": c.critique_score,
                        "defects": c.critique.get("defects"),
                        "targeted_fixes": c.critique.get("targeted_fixes"),
                    }
                )
        all_candidates.extend(candidates)

        ranked = sorted(candidates, key=lambda c: c.score, reverse=True)
        winner = ranked[0]
        for c in ranked:
            if c.ok_hard and (best_shippable is None or c.score > best_shippable.score):
                best_shippable = c

        if winner.ok_hard and winner.critique_ok:
            return _finalize(winner, request_text, attempts, all_candidates, critiques)

        feedback = (
            winner.feedback
            or (best_shippable.feedback if best_shippable else None)
            or "Redesign the part so it more clearly and correctly fulfills the request."
        )

    # Rounds exhausted. Valid geometry that never cleared the critique bar still
    # ships — a real, printable part beats nothing, and the founder review gate is
    # the final quality check.
    if best_shippable is not None:
        return _finalize(
            best_shippable,
            request_text,
            attempts,
            all_candidates,
            critiques,
            low_critique=True,
        )

    log_demand(request_text, "auto_generation_failed", {"attempts": attempts})
    return GenerationResult(
        ok=False,
        attempts=attempts,
        error=(
            "We couldn't design this automatically after "
            f"{MAX_ATTEMPTS} rounds. Your request has been logged for a human to look at."
        ),
    )


# Wire rehydration so a restart can still resolve stored freeform templates.
set_ephemeral_loader(load_ephemeral_from_disk)

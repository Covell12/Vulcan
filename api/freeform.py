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
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, create_model

from api import codegen_provider
from api import sandbox
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

MAX_ATTEMPTS = 3  # 1 initial + 2 self-repair retries (per the milestone)
MAX_BBOX_MM = 250.0
_VALID_TYPES = {"number", "integer", "boolean", "choice"}


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


def _dfm_feedback(dfm: dict[str, Any]) -> str:
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


def generate_and_register(
    request_text: str,
    photos: list[PhotoInput],
    dims_hints: list[dict[str, Any]] | None = None,
) -> GenerationResult:
    """Run the generate → verify → sandbox-build → DFM loop with self-repair.
    Returns a GenerationResult; on total failure logs to the demand log."""
    feedback: str | None = None
    attempts: list[dict[str, Any]] = []

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            gen = codegen_provider.generate_template(
                request_text, photos, dims_hints, retry_feedback=feedback
            )
        except codegen_provider.CodegenProviderError as e:
            attempts.append({"attempt": attempt, "stage": "provider", "error": str(e)})
            feedback = f"The generation service errored: {e}"
            continue

        code = gen.get("cadquery_code") or ""
        critical = list(gen.get("critical_dims") or [])
        assumptions = list(gen.get("assumptions") or [])
        overlays = _normalize_overlays(gen.get("overlays"))

        try:
            param_schema = normalize_param_schema(gen.get("param_schema") or [])
            _validate_candidate(code, param_schema, critical)
        except GenerationRejected as e:
            attempts.append({"attempt": attempt, "stage": "validate", "error": str(e)})
            feedback = str(e)
            continue

        with tempfile.TemporaryDirectory(prefix="vulcan_gen_") as tmp:
            result = sandbox.run_generated_build(
                code, default_params(param_schema), Path(tmp) / "out"
            )
            if not result.ok:
                attempts.append(
                    {"attempt": attempt, "stage": result.stage, "error": result.error}
                )
                feedback = f"The generated code failed to build ({result.stage}): {result.error}"
                continue

            dfm_ok, dfm = dfm_check(result.files["stl"])

        if not dfm_ok:
            attempts.append({"attempt": attempt, "stage": "dfm", "dfm": dfm})
            feedback = "The built part failed DFM checks: " + _dfm_feedback(dfm)
            continue

        # Success.
        gen_id = EPHEMERAL_PREFIX + uuid.uuid4().hex[:12]
        attempts.append({"attempt": attempt, "stage": "ok", "dfm": dfm})
        _persist(
            gen_id,
            code,
            param_schema,
            critical,
            {
                "request": request_text,
                "assumptions": assumptions,
                "critical_dims": critical,
                "overlays": overlays,
                "dfm": dfm,
                "attempts": attempts,
                "created_at": _now(),
            },
        )
        spec = _make_spec(gen_id, code, param_schema, critical)
        return GenerationResult(
            ok=True,
            template_id=gen_id,
            spec=spec,
            param_schema=param_schema,
            critical_dims=critical,
            assumptions=assumptions,
            overlays=overlays,
            dfm=dfm,
            attempts=attempts,
        )

    # All attempts failed.
    log_demand(
        request_text,
        "auto_generation_failed",
        {"attempts": attempts},
    )
    return GenerationResult(
        ok=False,
        attempts=attempts,
        error=(
            "We couldn't design this automatically after "
            f"{MAX_ATTEMPTS} attempts. Your request has been logged for a human to look at."
        ),
    )


# Wire rehydration so a restart can still resolve stored freeform templates.
set_ephemeral_loader(load_ephemeral_from_disk)

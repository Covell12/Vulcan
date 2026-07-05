"""Untrusted-code runner. Executed ONLY as an isolated `python -I` subprocess by
api/sandbox.py — never imported into the API process.

Contract: argv = [workdir, repo_root]. Reads `<workdir>/code.py` (the generated
CadQuery module) and `<workdir>/params.json`, then:
  1. re-verifies the code with api.code_verifier (defense-in-depth — the parent
     already verified it before spawning us),
  2. exec()s it in a locked-down namespace: a guarded `__import__` that only
     admits cadquery/math/numpy, and builtins with open/eval/exec/compile/input
     removed,
  3. calls `build(params)` and exports STEP/STL/3MF into `<workdir>/out/`,
  4. writes `<workdir>/result.json` with the outcome.

Untrusted stdout/stderr are silenced; all communication back to the parent is
via result.json, so a flood of prints can't matter. This runner itself is
trusted code and uses the real builtins (open, os, cadquery exporters); only the
exec'd generated code sees the restricted namespace.
"""

from __future__ import annotations

import json
import re
import sys
import traceback
from pathlib import Path

# A design may be a SINGLE solid or an ASSEMBLY of several parts that fit
# together. build(params) may return a cadquery Workplane (one part), a dict
# {"part_name": Workplane, ...}, or a list/tuple of Workplanes. Each part is
# exported to its OWN <name>.step/.stl/.3mf. Capped so a runaway can't write
# thousands of files.
_MAX_PARTS = 8
_PART_NAME_RE = re.compile(r"[^a-zA-Z0-9_]")

# Errors from the exec'd code that carry a build failure the self-repair loop
# can learn from are written to result.json; the process still exits 0 (the
# parent reads result.json, not the exit code).


def _safe_part_name(name: object, i: int) -> str:
    """Turn a model-supplied part name into a safe filename stem — alnum/underscore
    only, capped, never empty and never path-traversal (names become filenames)."""
    stem = _PART_NAME_RE.sub("_", str(name)).strip("_")[:40]
    return stem or f"part{i + 1}"


def _normalize_parts(result: object) -> list[tuple[str, object]]:
    """Normalize build()'s return into an ordered [(unique_safe_name, solid)]."""
    if isinstance(result, dict):
        raw = list(result.items())
    elif isinstance(result, (list, tuple)):
        raw = [(f"part{i + 1}", v) for i, v in enumerate(result)]
    else:
        raw = [("part", result)]
    seen: set[str] = set()
    parts: list[tuple[str, object]] = []
    for i, (k, v) in enumerate(raw):
        name = _safe_part_name(k, i)
        while name in seen:
            name = f"{name}_{i + 1}"
        seen.add(name)
        parts.append((name, v))
    return parts


def _write_result(
    workdir: Path,
    ok: bool,
    error: str | None = None,
    parts: list[str] | None = None,
) -> None:
    try:
        (workdir / "result.json").write_text(
            json.dumps({"ok": ok, "error": error, "parts": parts or []}),
            encoding="utf-8",
        )
    except Exception:
        pass


def _run(workdir: Path, repo_root: str) -> list[str]:
    sys.path.insert(0, repo_root)
    from api.code_verifier import ALLOWED_IMPORT_ROOTS, ENTRYPOINT, verify_code

    code = (workdir / "code.py").read_text(encoding="utf-8")
    params = json.loads((workdir / "params.json").read_text(encoding="utf-8"))

    # Belt to the parent's braces: reject again here, in the sandbox, before exec.
    verify_code(code)

    import builtins as _builtins

    real_import = _builtins.__import__

    def _guarded_import(name, *args, **kwargs):
        root = name.split(".")[0]
        if root not in ALLOWED_IMPORT_ROOTS:
            raise ImportError(f"import of '{name}' is blocked in the sandbox")
        return real_import(name, *args, **kwargs)

    safe_builtins = dict(vars(_builtins))
    for banned in (
        "open",
        "eval",
        "exec",
        "compile",
        "input",
        "breakpoint",
        "memoryview",
    ):
        safe_builtins.pop(banned, None)
    safe_builtins["__import__"] = _guarded_import

    ns: dict = {"__builtins__": safe_builtins, "__name__": "generated"}

    # Silence the untrusted code's stdout/stderr while it runs.
    import os

    devnull = open(os.devnull, "w")
    saved_out, saved_err = sys.stdout, sys.stderr
    try:
        sys.stdout = sys.stderr = devnull
        exec(compile(code, "<generated>", "exec"), ns)
        build = ns.get(ENTRYPOINT)
        if not callable(build):
            raise RuntimeError(f"generated code defines no callable {ENTRYPOINT}()")
        parts = _normalize_parts(build(params))
        if not parts:
            raise RuntimeError("build() returned no geometry")
        if len(parts) > _MAX_PARTS:
            raise RuntimeError(
                f"build() returned {len(parts)} parts; the maximum is {_MAX_PARTS}"
            )

        from cadquery import exporters

        out = workdir / "out"
        out.mkdir(exist_ok=True)
        names: list[str] = []
        for name, solid in parts:
            exporters.export(solid, str(out / f"{name}.step"))
            exporters.export(solid, str(out / f"{name}.stl"))
            exporters.export(solid, str(out / f"{name}.3mf"))
            names.append(name)
        return names
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err
        devnull.close()


def main() -> None:
    workdir = Path(sys.argv[1])
    repo_root = sys.argv[2]
    try:
        names = _run(workdir, repo_root)
        _write_result(workdir, True, parts=names)
    except Exception as e:  # noqa: BLE001 — any failure is a build failure
        detail = f"{type(e).__name__}: {e}"
        tb = traceback.format_exc()
        # Keep the last frames (where the error actually is), capped.
        _write_result(workdir, False, f"{detail}\n{tb[-2000:]}")


if __name__ == "__main__":
    main()

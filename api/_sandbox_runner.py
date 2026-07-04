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
import sys
import traceback
from pathlib import Path

# Errors from the exec'd code that carry a build failure the self-repair loop
# can learn from are written to result.json; the process still exits 0 (the
# parent reads result.json, not the exit code).


def _write_result(workdir: Path, ok: bool, error: str | None = None) -> None:
    try:
        (workdir / "result.json").write_text(
            json.dumps({"ok": ok, "error": error}), encoding="utf-8"
        )
    except Exception:
        pass


def _run(workdir: Path, repo_root: str) -> None:
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
        solid = build(params)

        from cadquery import exporters

        out = workdir / "out"
        out.mkdir(exist_ok=True)
        exporters.export(solid, str(out / "part.step"))
        exporters.export(solid, str(out / "part.stl"))
        exporters.export(solid, str(out / "part.3mf"))
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err
        devnull.close()


def main() -> None:
    workdir = Path(sys.argv[1])
    repo_root = sys.argv[2]
    try:
        _run(workdir, repo_root)
        _write_result(workdir, True)
    except Exception as e:  # noqa: BLE001 — any failure is a build failure
        detail = f"{type(e).__name__}: {e}"
        tb = traceback.format_exc()
        # Keep the last frames (where the error actually is), capped.
        _write_result(workdir, False, f"{detail}\n{tb[-2000:]}")


if __name__ == "__main__":
    main()

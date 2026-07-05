"""Run LLM-generated CadQuery code SAFELY, out of the API process (Track B).

Generated code is untrusted. It is NEVER executed in the API process. This
module is the containment boundary; its layers, outermost first:

  1. Static AST verification (api/code_verifier) — reject known-dangerous code
     BEFORE anything runs. Fail closed.
  2. A separate subprocess: `python -I` (isolated mode — no user site-packages,
     ignores PYTHON* env, script dir off sys.path), with a HARD wall-clock
     timeout, OS resource limits (CPU/file-size/#files/#procs via setrlimit),
     stdin/stdout/stderr on /dev/null, and an environment scrubbed of secrets.
  3. A runtime-locked exec namespace inside that subprocess (guarded __import__,
     no open/eval/exec) — see api/_sandbox_runner.py.

The subprocess exports STEP/STL/3MF into its OWN temp dir; only those geometry
files are copied back into the caller's output dir. No generated Python ever
crosses back into the API process.

On timeout the WHOLE process group is SIGKILLed (the child runs in its own
session), so a grandchild it spawned can't outlive the timeout.

HONEST LIMITATION (do not skip): this is static-analysis + process-isolation +
resource limits, NOT a syscall jail. It does NOT use containers/seccomp/gVisor.
A red-team pass confirmed that if the static verifier were bypassed, in-process
code could still read/write files and (fork limits permitting) exec — so the AST
layer's completeness is load-bearing. The hardened verifier now blocks every
escape chain that pass found, but a denylist is not a proof. THEREFORE: freeform
generation must NOT be exposed beyond localhost without a real OS-level sandbox
(seccomp/gVisor/container, no network, read-only FS). That, plus the founder
review gate (approval required before any file ships, protected by
VULCAN_REVIEW_TOKEN), is the Phase-0 posture. On macOS some setrlimit limits
(notably RLIMIT_AS) are not enforced — the timeout + process-group kill are the
primary bounds there; Linux enforces them all.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from api.code_verifier import UnsafeCodeError, verify_code

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
_RUNNER = _THIS_DIR / "_sandbox_runner.py"

DEFAULT_TIMEOUT_S = 30
# CPU seconds allowed the child — a hair above the wall-clock timeout so the
# wall-clock timeout is normally what fires, but a busy-loop that dodges it
# (e.g. blocked on the GIL) still gets killed.
_CPU_SECONDS = DEFAULT_TIMEOUT_S + 10
_MAX_OUTPUT_BYTES = 256 * 1024 * 1024  # per exported file; also the RLIMIT_FSIZE
_MAX_RESULT_BYTES = 1 * 1024 * 1024  # result.json is tiny; cap the parent's read
_MAX_PROCS = 64  # cap forks (crude fork-bomb guard)
_MAX_OPEN_FILES = 256

# Env var name fragments whose values are secrets and must not reach the child.
_SECRET_FRAGMENTS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "OPENAI",
    "ANTHROPIC",
    "REPLICATE",
    "AWS",
    "STRIPE",
)


class SandboxError(RuntimeError):
    """A sandbox-infrastructure failure (not a build failure of the generated
    code — those come back as SandboxResult(ok=False))."""


@dataclass(frozen=True)
class SandboxResult:
    ok: bool
    stage: str  # "verify" | "timeout" | "run" | "output" | "ok"
    error: str | None = None
    # One entry per exported part: {"name": str, "step"/"stl"/"threemf": Path}.
    # A single-solid design has exactly one part named "part".
    parts: list[dict] | None = None


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGKILL the child's whole process group (it was started with its own
    session), so any grandchildren it spawned die with it. Best-effort."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _clean_env() -> dict[str, str]:
    return {
        k: v
        for k, v in os.environ.items()
        if not any(frag in k.upper() for frag in _SECRET_FRAGMENTS)
    }


def _set_limits() -> None:  # pragma: no cover - runs in the forked child
    """preexec_fn: apply OS resource limits in the child before exec. Best-effort
    — a platform that rejects a given limit (macOS ignores some) just skips it;
    the wall-clock timeout is the guaranteed bound."""
    import resource

    limits = [
        ("RLIMIT_CPU", (_CPU_SECONDS, _CPU_SECONDS)),
        ("RLIMIT_FSIZE", (_MAX_OUTPUT_BYTES, _MAX_OUTPUT_BYTES)),
        ("RLIMIT_NPROC", (_MAX_PROCS, _MAX_PROCS)),
        ("RLIMIT_NOFILE", (_MAX_OPEN_FILES, _MAX_OPEN_FILES)),
    ]
    for name, value in limits:
        const = getattr(resource, name, None)
        if const is None:
            continue
        try:
            resource.setrlimit(const, value)
        except (ValueError, OSError):
            pass


def run_generated_build(
    code: str,
    params: dict,
    out_dir: Path,
    *,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> SandboxResult:
    """Verify, then run `build(params)` from `code` in the isolated subprocess,
    copying the exported STEP/STL/3MF into `out_dir`. Returns a SandboxResult;
    never raises for a *build* failure (bad geometry, exception, timeout) — those
    are `ok=False` with a `stage` and `error` the self-repair loop can use."""
    try:
        verify_code(code)
    except UnsafeCodeError as e:
        # The gate. Unsafe code is NEVER executed.
        return SandboxResult(ok=False, stage="verify", error=str(e))

    workdir = Path(tempfile.mkdtemp(prefix="vulcan_sbx_"))
    try:
        (workdir / "code.py").write_text(code, encoding="utf-8")
        (workdir / "params.json").write_text(json.dumps(params), encoding="utf-8")

        cmd = [sys.executable, "-I", str(_RUNNER), str(workdir), str(_REPO_ROOT)]
        preexec = _set_limits if os.name == "posix" else None
        # start_new_session=True puts the child in its OWN process group, so on
        # timeout we can SIGKILL the WHOLE group — otherwise a grandchild the code
        # spawned (fork/subprocess) would outlive the timeout (security review).
        proc = subprocess.Popen(
            cmd,
            cwd=str(workdir),
            env=_clean_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=preexec,
            start_new_session=True,
        )
        try:
            proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            return SandboxResult(
                ok=False,
                stage="timeout",
                error=f"generated code exceeded the {timeout_s}s time limit",
            )

        result_path = workdir / "result.json"
        if not result_path.exists():
            return SandboxResult(
                ok=False,
                stage="run",
                error="sandbox produced no result (it likely crashed or was killed)",
            )
        # Cap the read: the runner writes a small result, but the untrusted child
        # could have overwritten it with a huge file to OOM the parent.
        try:
            if result_path.stat().st_size > _MAX_RESULT_BYTES:
                return SandboxResult(
                    ok=False, stage="run", error="sandbox result was implausibly large"
                )
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            return SandboxResult(
                ok=False, stage="run", error=f"unreadable sandbox result: {e}"
            )

        if not result.get("ok"):
            return SandboxResult(
                ok=False,
                stage="run",
                error=str(result.get("error") or "generated code failed to build"),
            )

        part_names = result.get("parts") or ["part"]
        return _collect_outputs(workdir, out_dir, part_names)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9_]{1,40}$")


def _collect_outputs(workdir: Path, out_dir: Path, part_names: list) -> SandboxResult:
    """For EACH part, verify its three export files exist, are non-empty and under
    the size cap, then copy them into out_dir. Re-validates each part name against
    a strict allowlist (defense-in-depth: names came from the sandbox, but they
    originate in model output and become filenames here)."""
    src = workdir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    parts: list[dict] = []
    for raw in part_names:
        name = str(raw)
        if not _SAFE_NAME_RE.match(name):
            return SandboxResult(
                ok=False, stage="output", error=f"unsafe part name {name!r}"
            )
        entry: dict = {"name": name}
        for key, suffix in (("step", "step"), ("stl", "stl"), ("threemf", "3mf")):
            filename = f"{name}.{suffix}"
            s = src / filename
            if not s.exists() or s.stat().st_size == 0:
                return SandboxResult(
                    ok=False,
                    stage="output",
                    error=f"expected export '{filename}' is missing",
                )
            if s.stat().st_size > _MAX_OUTPUT_BYTES:
                return SandboxResult(
                    ok=False,
                    stage="output",
                    error=f"export '{filename}' exceeds the {_MAX_OUTPUT_BYTES}-byte cap",
                )
            dest = out_dir / filename
            shutil.copyfile(s, dest)
            entry[key] = dest
        parts.append(entry)
    if not parts:
        return SandboxResult(ok=False, stage="output", error="no parts were exported")
    return SandboxResult(ok=True, stage="ok", parts=parts)

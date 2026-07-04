"""Static AST safety check for LLM-generated CadQuery code (Track B).

This is the FIRST and most important line of defense for freeform generation:
untrusted, model-authored Python is parsed and structurally verified BEFORE it
is ever handed to the sandbox to run. If anything here is uncertain, the code
is rejected — fail closed.

Leaf module on purpose: it imports ONLY `ast` (stdlib), so both the API process
(api/sandbox.py) and the isolated sandbox subprocess (api/_sandbox_runner.py)
can import it without dragging in the rest of the app. Keep it dependency-free.

What it allows: a module that imports only the BARE modules `cadquery`, `math`,
`numpy` (no submodule imports), defines helpers/constants, and exposes a
`build(params)` function. What it rejects (with a specific, listable reason for
each): any other import, submodule imports, the classic escape primitives
(exec/eval/compile, open, __import__, getattr/setattr/delattr), ANY private or
dunder attribute (a leading underscore, e.g. `__globals__`, `_core`), and a
denylist of dangerous attribute / imported names (os, sys, subprocess, ctypes,
f2py, …).

Why the submodule + attribute rules are this strict: a red-team pass found a full
escape — a trusted submodule of an ALLOWED package transitively imports
os/subprocess, so an ordinary (non-dunder) attribute chain like
`cadquery.occ_impl.exporters.os` or `numpy._core.records.os` reached the REAL
os/subprocess and defeated the sandbox. Banning submodule imports + every private
(`._x`) attribute + the dangerous-name denylist closes every such path found in
the installed packages.

This is static analysis with a DENYLIST — it raises the bar a great deal but is
NOT a proof of safety. It is layered with runtime import guards + OS-level
subprocess isolation (api/sandbox.py). The DEFINITIVE containment is an OS-level
jail (seccomp/container); freeform must not be exposed beyond localhost without
one — see api/sandbox.py's limitation note and the milestone security notes.
"""

from __future__ import annotations

import ast

# The ONLY modules generated code may import — and ONLY as the bare top-level
# module (no submodule imports). `import numpy` is fine; `import numpy._core...`
# is rejected, because a trusted submodule of an allowed package (e.g.
# cadquery.occ_impl.exporters, numpy._core.records) transitively imports os /
# subprocess and would otherwise expose the REAL os/subprocess as an ordinary
# (non-dunder) attribute — a full sandbox escape. Submodule *math* (e.g.
# np.linalg) is still reachable via attribute access, which the attribute
# denylist + private-attribute ban below police.
ALLOWED_IMPORT_ROOTS = frozenset({"cadquery", "math", "numpy"})

# Attribute (and imported-name) DENYLIST: the terminal hop of every known escape
# chain is a real module/callable name. Reaching any of these — as an attribute
# of an allowed package or as a from-import name — is the escape, so we reject
# them outright. Combined with the private-attribute ban (any `._name`), this
# closes every submodule-attribute path found in the installed packages. It is a
# denylist, so it is NOT a proof of safety — see the module docstring + security
# notes: the definitive containment is an OS-level jail.
BANNED_ATTRS = frozenset(
    {
        # runtime modules that grant file/network/process/host access
        "os",
        "sys",
        "subprocess",
        "shutil",
        "tempfile",
        "importlib",
        "imp",
        "builtins",
        "socket",
        "ssl",
        "ctypes",
        "ctypeslib",
        "cffi",
        "pickle",
        "marshal",
        "shelve",
        "dbm",
        "code",
        "codeop",
        "pty",
        "platform",
        "sysconfig",
        "runpy",
        "multiprocessing",
        "threading",
        "asyncio",
        "concurrent",
        "resource",
        "signal",
        "io",
        "pathlib",
        "glob",
        "fileinput",
        "f2py",
        "distutils",
        "setuptools",
        "testing",
        "site",
        "gc",
        "inspect",
        "types",
        "warnings",
        "atexit",
        "weakref",
        "traceback",
        "linecache",
        "webbrowser",
        "urllib",
        "http",
        "ftplib",
        "smtplib",
        "telnetlib",
        "ossaudiodev",
        "nt",
        "posix",
        "_os",
        # dangerous callables
        "system",
        "popen",
        "popen2",
        "spawn",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnv",
        "spawnve",
        "spawnvp",
        "execv",
        "execve",
        "execl",
        "execlp",
        "execvp",
        "fork",
        "forkpty",
        "kill",
        "killpg",
        "putenv",
        "setuid",
        "exec",
        "eval",
        "compile",
        "open",
        "fdopen",
        "getattr",
        "setattr",
        "delattr",
        "globals",
        "locals",
        "vars",
        "input",
        "breakpoint",
        "__import__",
        # numpy file-IO / pickle vectors
        "load",
        "loads",
        "save",
        "savez",
        "savez_compressed",
        "tofile",
        "fromfile",
        "memmap",
        "loadtxt",
        "genfromtxt",
        "savetxt",
        "DataSource",
    }
)

# Bare names that must never be called or referenced — the usual escape hatches.
# `getattr`/`setattr`/`delattr` are here because they enable attribute smuggling
# (e.g. getattr(obj, "__globals__")); real CAD code has no need for them.
BANNED_NAMES = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "open",
        "input",
        "__import__",
        "globals",
        "locals",
        "vars",
        "getattr",
        "setattr",
        "delattr",
        "hasattr",
        "breakpoint",
        "memoryview",
        "help",
        "exit",
        "quit",
        "compile",
    }
)

# The build entry point every generated module must expose.
ENTRYPOINT = "build"


class UnsafeCodeError(ValueError):
    """Raised when generated code fails the static safety check. `violations`
    lists every problem found (not just the first), so the message is useful
    both to a human and as self-repair feedback to the model."""

    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        super().__init__(
            "generated code rejected by the safety verifier:\n- "
            + "\n- ".join(violations)
        )


def _is_dunder(name: str) -> bool:
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


class _SafetyVisitor(ast.NodeVisitor):
    """Walks the whole tree collecting every violation. Never raises mid-walk —
    it accumulates so the caller sees the full list."""

    def __init__(self) -> None:
        self.violations: list[str] = []

    def _flag(self, node: ast.AST, msg: str) -> None:
        line = getattr(node, "lineno", "?")
        self.violations.append(f"line {line}: {msg}")

    # --- imports -----------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.name
            root = name.split(".")[0]
            if root not in ALLOWED_IMPORT_ROOTS:
                self._flag(
                    node,
                    f"import of '{name}' is not allowed "
                    f"(allowed: {sorted(ALLOWED_IMPORT_ROOTS)})",
                )
            elif "." in name:
                self._flag(
                    node,
                    f"submodule import '{name}' is not allowed — import only the "
                    "bare module (a trusted submodule can expose os/subprocess)",
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        root = module.split(".")[0]
        if node.level and node.level > 0:
            self._flag(node, "relative imports are not allowed")
        elif root not in ALLOWED_IMPORT_ROOTS:
            self._flag(
                node,
                f"import from '{module}' is not allowed "
                f"(allowed: {sorted(ALLOWED_IMPORT_ROOTS)})",
            )
        elif "." in module:
            self._flag(
                node,
                f"import from submodule '{module}' is not allowed (a trusted "
                "submodule can expose os/subprocess as a name)",
            )
        # Even `from cadquery import X`: block importing a dangerous/private name.
        for alias in node.names:
            if alias.name in BANNED_ATTRS or alias.name.startswith("_"):
                self._flag(node, f"importing name '{alias.name}' is not allowed")
        self.generic_visit(node)

    # --- name / attribute smuggling ---------------------------------------
    def visit_Name(self, node: ast.Name) -> None:
        if node.id in BANNED_NAMES:
            self._flag(node, f"use of '{node.id}' is not allowed")
        elif _is_dunder(node.id):
            self._flag(node, f"reference to dunder name '{node.id}' is not allowed")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # Any private/dunder attribute (leading underscore) — this alone blocks
        # the `numpy._core.*` family and every `.__x__` smuggle.
        if node.attr.startswith("_"):
            self._flag(
                node,
                f"access to private/dunder attribute '.{node.attr}' is not allowed "
                "(attribute-smuggling vector)",
            )
        elif node.attr in BANNED_ATTRS:
            self._flag(
                node,
                f"access to attribute '.{node.attr}' is not allowed — it can reach "
                "os/subprocess/file-IO from within an allowed package",
            )
        self.generic_visit(node)

    # --- misc dangerous constructs ----------------------------------------
    def visit_Global(self, node: ast.Global) -> None:
        self._flag(node, "`global` statements are not allowed")
        self.generic_visit(node)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self._flag(node, "`nonlocal` statements are not allowed")
        self.generic_visit(node)


def find_violations(code: str) -> list[str]:
    """Return a list of safety violations in `code` (empty means it passed the
    static check). A syntax error is itself a violation."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"line {e.lineno}: syntax error: {e.msg}"]

    visitor = _SafetyVisitor()
    visitor.visit(tree)

    violations = list(visitor.violations)
    if not _defines_entrypoint(tree):
        violations.append(
            f"code must define a top-level `def {ENTRYPOINT}(params):` function"
        )
    return violations


def _defines_entrypoint(tree: ast.Module) -> bool:
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == ENTRYPOINT
        for node in tree.body
    )


def verify_code(code: str) -> None:
    """Raise UnsafeCodeError if `code` fails the static safety check. This is the
    gate: callers MUST run it (and let it raise) before executing generated
    code anywhere."""
    violations = find_violations(code)
    if violations:
        raise UnsafeCodeError(violations)

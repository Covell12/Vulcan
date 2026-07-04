"""Tests for api/code_verifier.py — the static AST safety gate for Track B
freeform generation. These are security tests: every known escape shape must be
rejected, and legitimate CadQuery code must pass.
"""

from __future__ import annotations

import pytest

from api.code_verifier import UnsafeCodeError, find_violations, verify_code

GOOD = """
import cadquery as cq
import math
import numpy as np

WALL = 2.4

def _profile(w, h):
    return [(0, 0), (w, 0), (w, h), (0, h)]

def build(params):
    w = float(params["width_mm"])
    h = float(params["height_mm"])
    pts = _profile(w, h)
    return cq.Workplane("XY").polyline(pts).close().extrude(params["depth_mm"])
"""


def test_good_code_passes():
    assert find_violations(GOOD) == []
    verify_code(GOOD)  # does not raise


def test_numpy_math_reachable_via_attribute():
    # numpy math is used via attribute access (np.linspace, np.linalg.norm), which
    # is the legit path — no submodule import needed.
    code = (
        "import numpy as np\n"
        "import math\n"
        "def build(params):\n"
        "    return np.linalg.norm(np.linspace(0, math.pi, 5))\n"
    )
    assert find_violations(code) == []


def test_submodule_import_rejected():
    # SECURITY: a trusted submodule of an allowed package can expose os/subprocess
    # as an ordinary attribute, so submodule imports are banned entirely.
    for code in (
        "import numpy.linalg\ndef build(params):\n    return None\n",
        "import cadquery.occ_impl.exporters\ndef build(params):\n    return None\n",
        "import numpy._core.records as r\ndef build(params):\n    return None\n",
    ):
        assert find_violations(code), f"submodule import should be rejected: {code!r}"


@pytest.mark.parametrize(
    "snippet",
    [
        # The verified red-team escapes: reach the real os/subprocess through a
        # non-dunder submodule-attribute chain of an allowed package.
        "import cadquery.occ_impl.exporters as ex\ndef build(params):\n    return ex.os\n",
        "import cadquery as cq\ndef build(params):\n    return cq.occ_impl.exporters.os\n",
        "import numpy as np\ndef build(params):\n    return np._core.records.os\n",
        "import numpy as np\ndef build(params):\n    return np.f2py.subprocess\n",
        "from cadquery.occ_impl.exporters import os\ndef build(params):\n    return os\n",
        "from cadquery import os\ndef build(params):\n    return os\n",
        "import numpy as np\ndef build(params):\n    return np.ctypeslib\n",
        "import numpy as np\ndef build(params):\n    return np.load('/etc/passwd')\n",
        "import cadquery as cq\ndef build(params):\n    return cq.sys\n",
    ],
)
def test_submodule_attribute_escape_chains_rejected(snippet: str):
    assert find_violations(snippet), f"escape chain must be rejected: {snippet!r}"


@pytest.mark.parametrize(
    "snippet",
    [
        "import os",
        "import os.path",
        "import sys",
        "import subprocess",
        "import socket",
        "from os import system",
        "from subprocess import run",
        "import requests",
        "from . import helpers",
        "from .sibling import x",
    ],
)
def test_disallowed_imports_rejected(snippet: str):
    code = f"{snippet}\ndef build(params):\n    return None\n"
    violations = find_violations(code)
    assert violations, f"expected {snippet!r} to be rejected"


@pytest.mark.parametrize(
    "snippet",
    [
        "open('/etc/passwd')",
        "eval('1+1')",
        "exec('x=1')",
        "compile('1', '<s>', 'eval')",
        "__import__('os')",
        "getattr(cq, 'foo')",
        "setattr(cq, 'foo', 1)",
        "globals()",
        "vars()",
        "breakpoint()",
    ],
)
def test_banned_names_rejected(snippet: str):
    code = f"def build(params):\n    {snippet}\n    return None\n"
    assert find_violations(code), f"expected {snippet!r} to be rejected"


@pytest.mark.parametrize(
    "snippet",
    [
        "().__class__.__bases__",
        "cq.__globals__",
        "x.__subclasses__()",
        "obj.__dict__",
        "type.__mro__",
        "f.__code__",
    ],
)
def test_dunder_attribute_smuggling_rejected(snippet: str):
    code = f"def build(params):\n    y = {snippet}\n    return None\n"
    assert find_violations(code), f"expected {snippet!r} to be rejected"


def test_dunder_name_reference_rejected():
    code = "def build(params):\n    return __builtins__\n"
    assert find_violations(code)


def test_global_and_nonlocal_rejected():
    code = "X = 1\ndef build(params):\n    global X\n    X = 2\n    return None\n"
    assert find_violations(code)


def test_missing_build_entrypoint_rejected():
    code = "import cadquery as cq\ndef helper():\n    return 1\n"
    v = find_violations(code)
    assert any("build" in x for x in v)


def test_syntax_error_is_a_violation():
    v = find_violations("def build(params:\n")
    assert v and "syntax error" in v[0]


def test_all_violations_collected_not_just_first():
    code = "import os\nimport socket\ndef build(params):\n    return open('x')\n"
    v = find_violations(code)
    assert len(v) >= 3  # os, socket, open


def test_verify_code_raises_with_violations_listed():
    with pytest.raises(UnsafeCodeError) as exc:
        verify_code("import os\ndef build(params):\n    return None\n")
    assert exc.value.violations
    assert "os" in str(exc.value)

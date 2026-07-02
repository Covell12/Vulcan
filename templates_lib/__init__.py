"""Importing this package registers every template in templates_lib.registry.

Each template module registers itself as a side effect of being imported (see
the bottom of each template file). Anything that needs the full registry
populated — the API, the test suite — should `import templates_lib` (or import
any of its submodules that itself triggers this package import) before using
`templates_lib.registry`.
"""

from templates_lib import adapter_tube as _adapter_tube  # noqa: F401
from templates_lib import bracket_shelf_l as _bracket_shelf_l  # noqa: F401
from templates_lib import knob_appliance as _knob_appliance  # noqa: F401

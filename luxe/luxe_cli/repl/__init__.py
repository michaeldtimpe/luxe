"""Interactive REPL package.

Phase 6 split: the 2000-line monolith was factored into cohesive modules.
The public surface is unchanged — `from luxe import repl; repl.start(...)`
still works because `start` is re-exported here.
"""

from luxe_cli.repl.core import start

__all__ = ["start"]

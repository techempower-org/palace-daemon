"""Backwards-compatibility shim — see :mod:`mempalace.kg_canonical_writepass`.

The canonical implementation moved to the mempalace package via
mempalace#281 / techempower-org/mempalace#290 to escape the
PYTHONPATH-strip bug. ``mempalace/__init__.py`` strips PYTHONPATH-derived
``sys.path`` entries for ABI hygiene, which silently broke the bare
top-level ``from kg_canonical_writepass import ...`` the live write path
used to do — the import failed quietly into an identity-fallback stub
and the canonical mapping never landed on edges.

This shim preserves the bare top-level import for palace-daemon's own
tests and any historical callers that still import via ``from
kg_canonical_writepass import ...``. New code should prefer the
package-qualified form ``from mempalace.kg_canonical_writepass import
...`` directly. The shim is slated for removal once all callers
migrate.

See:
- techempower-org/palace-daemon#86 (this shim)
- techempower-org/mempalace#290 (the port that moved the source)
- techempower-org/mempalace#281 (the bug that motivated the move)
"""
from mempalace.kg_canonical_writepass import *  # noqa: F401,F403

import mempalace.kg_canonical_writepass as _impl


def __getattr__(name):
    """Forward any name not exported via ``*`` to the real impl.

    Lets ``import kg_canonical_writepass as wp; wp._FLAG`` and similar
    underscore-prefixed-attribute access keep working in palace-daemon's
    tests. PEP 562 module-level ``__getattr__`` requires Python 3.7+;
    palace-daemon's floor is well past that.
    """
    try:
        return getattr(_impl, name)
    except AttributeError:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        ) from None

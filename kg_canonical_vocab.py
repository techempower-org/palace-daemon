"""Backwards-compatibility shim — see :mod:`mempalace.kg_canonical_vocab`.

The canonical implementation moved to the mempalace package via
mempalace#281 / techempower-org/mempalace#290. This shim preserves the
bare top-level import for palace-daemon's own tests and any historical
callers. New code should prefer the package-qualified form ``from
mempalace.kg_canonical_vocab import ...`` directly.

The post-#85 batched ``CanonicalMapper.map_predicates`` API is included
in the ported version, so callers that need the bulk path get it
through the shim unchanged.

See:
- techempower-org/palace-daemon#86 (this shim)
- techempower-org/palace-daemon#85 (added map_predicates)
- techempower-org/mempalace#290 (the port that moved the source)
"""
from mempalace.kg_canonical_vocab import *  # noqa: F401,F403

import mempalace.kg_canonical_vocab as _impl


def __getattr__(name):
    """Forward any name not exported via ``*`` to the real impl. See
    ``kg_canonical_writepass.py``'s docstring for the rationale.
    """
    try:
        return getattr(_impl, name)
    except AttributeError:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        ) from None

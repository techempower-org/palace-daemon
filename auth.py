"""Auth helpers — extracted from main.py per #101 (ninth slice).

Owns two surfaces:

1. ``check_auth(x_api_key)`` — the strict header check used by every
   write endpoint and most reads. No-ops when ``PALACE_API_KEY`` is
   unset (development mode). Otherwise enforces constant-time equality
   between the header value and the env key.

2. ``mint_viz_token`` / ``valid_viz_token`` / ``check_viz_auth`` — the
   relaxed auth surface for the read-only `/viz` dashboard, which needs
   to be bookmarkable without putting the long-lived API key in the URL.
   The page POSTs the key once to ``/viz/session`` and gets back a
   short-lived HttpOnly SameSite=Lax cookie. The token is signed with
   the API key itself (HMAC-SHA256), so no second secret is needed.

Re-exported from main.py under the original ``_``-prefixed names so the
route handlers keep working. The ``PALACE_VIZ_SESSION_TTL_SECONDS`` and
``PALACE_VIZ_COOKIE_SECURE`` constants live here now too — the one test
that patches the TTL (``tests/test_viz_session_auth.py``) was updated to
patch ``auth.PALACE_VIZ_SESSION_TTL_SECONDS`` rather than ``main``.

PALACE_API_KEY is intentionally read via ``os.getenv`` at every call,
not cached at module-load time. This lets tests inject the key via
``patch.dict(os.environ, ...)`` without monkeying with module state.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time

from fastapi import HTTPException


VIZ_COOKIE_NAME = "palace_viz_session"
PALACE_VIZ_SESSION_TTL_SECONDS = int(os.getenv("PALACE_VIZ_SESSION_TTL_SECONDS", "28800"))  # 8h
# Set when the daemon is reached over https (e.g. behind a TLS reverse proxy) so
# the cookie carries the Secure attribute. Default off to not break local http.
PALACE_VIZ_COOKIE_SECURE = os.getenv("PALACE_VIZ_COOKIE_SECURE", "").lower() in ("1", "true", "yes")


def check_auth(x_api_key: str | None):
    """Strict header check used by write endpoints. Raises 401 on mismatch.

    No-op when PALACE_API_KEY is unset (development mode). When set, uses
    hmac.compare_digest for constant-time equality so wrong-key attempts
    can't be distinguished from no-key attempts via timing.
    """
    key = os.getenv("PALACE_API_KEY", "")
    if not key:
        return
    # hmac.compare_digest requires both arguments to be the same type and
    # non-None. Treat a missing header as an empty string so we always run
    # the constant-time path — short-circuiting on ``x_api_key is None``
    # would reintroduce a timing distinction between "no header" and
    # "wrong header".
    provided = x_api_key or ""
    if not hmac.compare_digest(provided, key):
        raise HTTPException(status_code=401, detail="Invalid API key")


def mint_viz_token() -> str:
    """Sign ``<expiry>.<hex_hmac>`` with PALACE_API_KEY. Caller guarantees the key is set."""
    key = os.getenv("PALACE_API_KEY", "").encode()
    exp = str(int(time.time()) + PALACE_VIZ_SESSION_TTL_SECONDS)
    sig = hmac.new(key, exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def valid_viz_token(token: str | None) -> bool:
    """True iff the token is well-formed, unexpired, and the HMAC verifies."""
    key = os.getenv("PALACE_API_KEY", "")
    if not key or not token or "." not in token:
        return False
    exp, _, sig = token.partition(".")
    try:
        if int(exp) < int(time.time()):
            return False
    except ValueError:
        return False
    expected = hmac.new(key.encode(), exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def check_viz_auth(x_api_key: str | None, session: str | None) -> None:
    """Auth for the read-only /viz surface: accept the X-Api-Key header OR a
    valid signed viz session cookie. No-op when PALACE_API_KEY is unset.

    Only used by GET endpoints the dashboard reads (/viz, /graph,
    /backfill-age/status). Write endpoints stay header-only so the cookie can
    never be replayed cross-site against a state-changing route (SameSite=Lax
    plus header-only writes = no CSRF surface)."""
    key = os.getenv("PALACE_API_KEY", "")
    if not key:
        return
    if x_api_key and hmac.compare_digest(x_api_key, key):
        return
    if valid_viz_token(session):
        return
    raise HTTPException(status_code=401, detail="Invalid API key")

"""Vercel Python entrypoint.

Vercel loads the top-level ``app`` object from this file and routes every
request to it.  The bot itself lives in ``converter_bot/`` as a flat module
tree, so that directory is added to ``sys.path`` before importing the ASGI
application.
"""

from __future__ import annotations

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
_PACKAGE_ROOT = os.path.join(_PROJECT_ROOT, "converter_bot")

if _PACKAGE_ROOT not in sys.path:
    sys.path.insert(0, _PACKAGE_ROOT)

from webhook import app  # noqa: E402  (path bootstrap must come first)

__all__ = ["app"]

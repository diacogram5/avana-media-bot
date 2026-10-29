"""Database package: the implementation is app.db (stdlib sqlite3) plus
versioned SQL in migrations/. This package re-exports it for a stable
import path.
"""
from ..db import *  # noqa: F401,F403
from ..db import connect, init_db, apply_migrations

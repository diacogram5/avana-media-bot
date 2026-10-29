"""Workers package (stage 1): temp-file cleanup is a function, not a daemon.

A scheduled cleanup job can be added later (cron/systemd) calling
``app.media.cleanup_temp_files``; no background worker is required to run
the bot itself, which keeps stage 1 simple and stable.
"""
from ..media import cleanup_temp_files

__all__ = ["cleanup_temp_files"]

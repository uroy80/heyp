"""Client for heyp's renderer sidecar (``services/renderer``: Chromium over a Unix socket). Synchronous,
cancellable, size-capped and never retrying; ``app/tools/render.py`` (HTML → image) uses it."""

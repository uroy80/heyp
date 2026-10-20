"""Image-op plug-ins (PLAN.md §3 "Ops"): every module here is imported by ``opreg.load_plugins()``.

Importing ``app.schemas`` first means that whichever module is imported first (a plug-in, the
schemas or the pipeline), every plug-in has registered before the request ``Op`` union is built.
Plug-ins must import only ``app.imaging.opreg`` (plus imaging helpers / ``app.schemas`` models),
never ``app.imaging.pipeline`` or ``app.api``.
"""
import app.schemas  # noqa: F401  (loads all plug-ins, then builds the Op union)

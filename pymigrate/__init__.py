"""Public library entry points for the migration pipeline.

Use ``create_plan`` to inspect a directory without modifying it, ``apply_plan``
to verify and apply that in-memory plan, and ``verify_repository`` to check the
current source files without executing them. CLI-specific formatting stays in
the ``cli`` module.
"""

from .pipeline import apply_plan, create_plan, verify_repository

# Keep the intended package-level API explicit for callers and documentation tools.
__all__ = ["apply_plan", "create_plan", "verify_repository"]

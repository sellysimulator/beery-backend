"""Re-export of the declarative `Base` for Alembic and the model modules.

This must import from `..models.base`. Writing `from .base import Base` here is a
self-import that raises ImportError on any use.
"""

from ..models.base import Base

__all__ = ["Base"]

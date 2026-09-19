"""The ORM model package.

Importing this package imports every model module in it, which is what puts
their tables into ``Base.metadata``.  Alembic's ``env.py`` and any autogenerate
run depend on that: without it ``Base.metadata`` is empty in a fresh process and
``alembic revision --autogenerate`` emits a ``drop_table`` for every table that
actually exists.

Discovery rather than a literal import list, for the same reason the three
assembly points in **D19** use it -- a later section adds a model module by
dropping the file in, and edits nothing here.
"""

import importlib
import pkgutil

for _, _name, _ in pkgutil.iter_modules(__path__):
    importlib.import_module(f"{__name__}.{_name}")

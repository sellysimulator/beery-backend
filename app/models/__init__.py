"""The ORM model package.

Importing this package imports every model module in it, which is what puts
their tables into ``Base.metadata``.  Alembic's ``env.py`` and any autogenerate
run depend on that: without it ``Base.metadata`` is empty in a fresh process and
``alembic revision --autogenerate`` emits a ``drop_table`` for every table that
actually exists.

Discovery rather than a literal import list, for the same reason the three
assembly points in **D19** use it -- a model module is added by dropping the
file in, and nothing here is edited.
"""

import importlib
import pkgutil

for _module in pkgutil.iter_modules(__path__):
    importlib.import_module(f"{__name__}.{_module.name}")

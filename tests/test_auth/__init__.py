"""Section 02 test package.

The ``__init__.py`` is load-bearing: without it pytest's prepend import mode
gives this package's ``conftest.py`` the top-level module name ``conftest``,
which collides with the shared ``tests/conftest.py`` and breaks section 01's
``from conftest import FakeRedis``.
"""

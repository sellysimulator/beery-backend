"""Marks ``tests/test_skeleton`` as a package.

Without this marker pytest inserts each test directory onto ``sys.path`` in
turn, and the bare module name ``conftest`` then resolves to whichever
directory was inserted first -- so a sibling package adding its own
``tests/test_<area>/conftest.py`` silently rebinds
``from conftest import FakeRedis`` here and collection breaks across the whole
suite.  With the marker present the first non-package ancestor is ``tests/``,
``conftest`` is unambiguous, and each package's own ``conftest.py`` still
applies only to its own directory.
"""

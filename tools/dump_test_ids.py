"""Dump every collected test as ClassName.method_name, sorted.

Used to prove that a pure move of test classes between modules changes
nothing about what is collected. Deliberately without the module path: a
move changes exactly that, and nothing else.

    python manage.py shell -v 0 -c "$(cat tools/dump_test_ids.py)"

Going through `manage.py` is required — it loads `.env` before Django
starts, and `-v 0` keeps shell's own banner off stdout.
"""

import unittest

from django.test.runner import DiscoverRunner


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


suite = DiscoverRunner(verbosity=0).build_suite(["projects"])
print(
    "\n".join(sorted(f"{type(t).__name__}.{t._testMethodName}" for t in flatten(suite)))
)

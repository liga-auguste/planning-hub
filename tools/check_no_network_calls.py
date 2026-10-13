"""Run the test suite and fail if any test reaches the network (#302).

A test that patches the wrong module path patches nothing, and the call it
meant to replace goes out for real. Locally that call succeeds on the `.env`
keys; in CI it fails on the throwaway ones — often with exactly the error the
missing mock was supposed to raise, so the test stays green either way. Grep
cannot find these: a patch target can be built as an f-string.

This catches them where they happen instead. For the length of the run it
replaces httpx's transport, which is where both the Notion SDK and the
Anthropic SDK send their requests, records the running test and the host,
and fails the request as a connection error. Exit status 1 if anything was
recorded.

Run it the way CI runs the suite — past `manage.py`, so no `.env` is read —
once per backend:

    ANTHROPIC_API_KEY=invalid-for-test NOTION_API_KEY=invalid-for-test \\
    DEMO_MODE=true SECRET_KEY=ci-test-key \\
    .venv/bin/python tools/check_no_network_calls.py [test labels]

With no labels it runs `projects`. `DEMO_MODE=false` selects PostgreSQL.
"""

import os
import sys
import unittest
from pathlib import Path

import httpx
from django.conf import settings
from django.core.management import execute_from_command_line
from django.test.runner import DiscoverRunner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "planning_hub.settings")

current_test = None
escaped = []


def _record(request):
    escaped.append((current_test, request.url.host))
    return httpx.ConnectError("blocked by check_no_network_calls", request=request)


def _blocked(self, request):
    raise _record(request)


async def _blocked_async(self, request):
    raise _record(request)


httpx.HTTPTransport.handle_request = _blocked
httpx.AsyncHTTPTransport.handle_async_request = _blocked_async


class _TrackingResult(unittest.TextTestResult):
    def startTest(self, test):
        global current_test
        current_test = test.id()
        super().startTest(test)


DiscoverRunner.get_resultclass = lambda self: _TrackingResult


def main():
    # Through the `test` command rather than a bare DiscoverRunner:
    # settings.py recognises a test run by sys.argv[1] == "test" (_TESTING)
    # and configures storage and HTTPS differently for it.
    # sys.argv itself is rewritten, because that is what settings.py reads.
    sys.argv = [sys.argv[0], "test", *(sys.argv[1:] or ["projects"]), "--noinput"]
    try:
        execute_from_command_line(sys.argv)
        failed = False
    except SystemExit as exit_:
        failed = bool(exit_.code)
    db = settings.DATABASES["default"]["ENGINE"].rsplit(".", 1)[-1]
    if escaped:
        print(f"\n{len(escaped)} request(s) reached the network ({db}):")
        for test_id, host in sorted(set(escaped), key=lambda e: (e[0] or "", e[1])):
            print(f"  {test_id or '<outside any test>'} -> {host}")
        return 1
    print(f"\nNo request reached the network ({db}).")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

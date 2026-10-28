"""Shared PostgreSQL fixtures. Skipped automatically without DATABASE_URL."""

from __future__ import annotations

import gc
import os
import sys
import traceback
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from support import TEST_CIPHER
from waxprep.migrations import apply_migrations
from waxprep.storage.postgres import PostgresStorage

DATABASE_URL = os.environ.get("DATABASE_URL", "")


@pytest.fixture
def pg_conn() -> Iterator[psycopg.Connection[Any]]:
    """A clean, fully migrated database for each test."""
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set")
    conn: psycopg.Connection[Any] = psycopg.connect(DATABASE_URL, autocommit=False)
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE")
        cur.execute("CREATE SCHEMA public")
    conn.commit()
    apply_migrations(conn)
    yield conn
    conn.close()


@pytest.fixture
def pg_store(pg_conn: psycopg.Connection[Any]) -> PostgresStorage:
    return PostgresStorage(pg_conn, TEST_CIPHER)


# ---------------------------------------------------------------------------
# Unraisable-exception observability (issue #13)
#
# ``filterwarnings = ["error"]`` turns unraisable exceptions (raised from
# ``__del__`` during garbage collection) into test failures. CPython can
# finalize a leaked object long after the test that created it, so the
# failure is usually pinned to an unrelated, allocation-heavy test. Three
# channels make the real source diagnosable from the gate's log tail:
#
# 1. A module-level chained ``sys.unraisablehook`` records every fire,
#    wherever CPython raises it.
# 2. An autouse fixture runs ``gc.collect()`` after every test, so each
#    leak surfaces in the test that created it, not in a later one.
# 3. ``pytest_runtest_makereport`` captures the full exception text of
#    unraisable failures, including sub-exception messages.
#
# Every printed line embeds "FAILED" and "Traceback" so the gate's log
# tail keeps the lines it otherwise filters out.
# ---------------------------------------------------------------------------

_UNRAISABLE_DETAILS: list[str] = []
_DETAIL_LIMIT = 90


def _describe_unraisable(unraisable: sys.UnraisableHookArgs) -> str:
    name = getattr(unraisable.exc_type, "__name__", "Exception")
    obj = repr(unraisable.object)
    if len(obj) > 200:
        obj = obj[:200] + "..."
    err = unraisable.err_msg or ""
    return f"HOOK {name}: {unraisable.exc_value!r} | object={obj} | err_msg={err}"


_DEFAULT_UNRAISABLE_HOOK = sys.unraisablehook


def _chained_unraisable_hook(unraisable: sys.UnraisableHookArgs) -> None:
    try:
        if len(_UNRAISABLE_DETAILS) < _DETAIL_LIMIT:
            _UNRAISABLE_DETAILS.append(_describe_unraisable(unraisable))
    except Exception:
        # Observability must never mask the hook it wraps.
        pass
    try:
        _DEFAULT_UNRAISABLE_HOOK(unraisable)
    except Exception:
        pass


# Installed at import time so the chain stays in place under pytest's
# own per-phase hook and records fires wherever CPython raises them.
sys.unraisablehook = _chained_unraisable_hook


@pytest.fixture(autouse=True)
def _finalize_leaks_after_test() -> Iterator[None]:
    """Collect garbage after each test so leaks fail their own test.

    Without this, a leaked object finalizes during some later,
    allocation-heavy test and the "unraisable exception" failure lands
    on that unrelated test instead of the leaking one.
    """
    yield
    gc.collect()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: Any, call: Any) -> Any:
    outcome = yield
    report: Any = outcome.get_result()
    if not report.failed:
        return
    text = str(report.longrepr)
    exc = getattr(call, "excinfo", None)
    exc_text = ""
    if exc is not None:
        try:
            exc_text = "".join(
                traceback.format_exception(
                    type(exc.value), exc.value, exc.value.__traceback__
                )
            )
        except Exception:
            exc_text = ""
    if "unraisable" not in text.lower() and "unraisable" not in exc_text.lower():
        return
    when = getattr(report, "when", "?")
    _UNRAISABLE_DETAILS.append(f"FAILED-REPORT {item.nodeid} phase={when}")
    seen: set[str] = set()
    for source in (exc_text, text):
        for line in source.splitlines():
            stripped = line.strip()
            if not stripped or stripped in seen:
                continue
            seen.add(stripped)
            if len(_UNRAISABLE_DETAILS) < _DETAIL_LIMIT:
                _UNRAISABLE_DETAILS.append("SUBEXC " + stripped[:300])


def pytest_terminal_summary(
    terminalreporter: Any, exitstatus: Any, config: Any
) -> None:
    # Finalize anything still alive; the module-level hook records it.
    gc.collect()
    for detail in _UNRAISABLE_DETAILS[:_DETAIL_LIMIT]:
        terminalreporter.write_line("UNRAISABLE-FAILED-Traceback " + detail[:400])
    extra = len(_UNRAISABLE_DETAILS) - _DETAIL_LIMIT
    if extra > 0:
        terminalreporter.write_line(f"UNRAISABLE-FAILED-Traceback ... {extra} more")

"""Shared pytest fixtures and test-isolation helpers for the ``tests/`` suite.

The agent runtime keeps a handful of process-wide ``asyncio`` primitives in
``cyrene.agent.state`` (a global ``_agent_lock``, an ``_interrupt_event`` and a
few sets of fire-and-forget background tasks). The test suite runs with
``asyncio_mode = auto`` (see ``pytest.ini``), which gives every test its own
event loop and tears that loop down when the test finishes.

Several tests spawn detached background tasks (session-label refreshes, the
main-inbox worker, behavior-learning kicks, ...) via ``_run_chat_agent`` and
friends. Those tasks are never awaited or cancelled, so a test can finish while
a task is still parked inside ``async with _agent_lock:``. When that test's loop
is closed, the ``async with`` release never runs and the global lock is left
stale-locked (``_agent_lock._locked is True``) for the rest of the process.

A later test (e.g. ``test_interrupt_active_run_clears_after_locked_run_finishes``)
then does ``async with _agent_lock`` on its own fresh loop, blocks forever on the
stale lock, and the whole run hangs.

This is purely a cross-test isolation artifact, not a production bug: in a real
long-lived event loop the ``async with`` always releases (normally or via
cancellation). The autouse fixture below forcibly resets the shared state before
every test so one test can never poison the next.
"""

import sys
from pathlib import Path

import pytest

# Tests import ``cyrene`` from the in-repo ``src/`` tree; make sure it is on the
# path before any cyrene import happens (mirrors the shim at the top of
# ``tests/test_runtime_fixes.py``).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


@pytest.fixture(autouse=True)
def _reset_agent_global_state():
    """Force-reset process-wide agent state before each test (setup phase).

    Always access the globals through the module object (``_state._agent_lock``
    etc.) rather than ``from ... import _agent_lock``: they are reassigned/mutated
    at runtime, and a from-import would bind a stale local copy.
    """
    from cyrene.agent import state as _state

    def _cancel_pending_tasks(tasks) -> None:
        # Mirror ``session.clear_session_id._cancel_pending_tasks``: a task may
        # live on an already-closed loop, so guard both ``done()`` and the
        # loop's ``is_closed()`` and swallow the RuntimeError that a closed loop
        # can raise.
        for task in list(tasks):
            try:
                if not task.done() and not task.get_loop().is_closed():
                    task.cancel()
            except RuntimeError:
                pass
        tasks.clear()

    # 1. Drain any stale lock. ``asyncio.Lock`` is not owned by a task, so
    #    ``release()`` simply flips the internal ``_locked`` flag and may be
    #    called from any task. It only raises when ``_locked`` is already False,
    #    so gate every release on ``locked()``.
    while _state._agent_lock.locked():
        _state._agent_lock.release()

    # The session-state lock is far less likely to leak, but reset it too so a
    # parked ``clear_session_id`` cannot deadlock a later test.
    while _state._session_state_lock.locked():
        _state._session_state_lock.release()

    # 2. Clear the interrupt event so a leaked "interrupt requested" flag from a
    #    previous test does not bleed into this one.
    _state._interrupt_event.clear()

    # 3/4. Cancel + clear all fire-and-forget task registries.
    _cancel_pending_tasks(_state._pending_interrupt_clearers)
    _cancel_pending_tasks(_state._pending_label_refreshes)
    _cancel_pending_tasks(_state._pending_compressors)

    if _state._main_inbox_worker is not None:
        _cancel_pending_tasks({_state._main_inbox_worker})
        _state._main_inbox_worker = None

    yield

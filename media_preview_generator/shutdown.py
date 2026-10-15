"""Process-wide "the container is stopping" flag, and the work the app does once when it begins stopping.

A stop signals FFmpeg children together with the app, so a file whose FFmpeg run died with it looks like a failed file.
Once this flag is set, work interrupted by the stop is neither recorded as a failure nor used to complete its job:
the job stays RUNNING in the store and ``JobManager.requeue_interrupted_jobs`` revives it on the next start.
"""

import atexit
import os
import signal
import threading
import time
from collections.abc import Callable
from types import FrameType

from loguru import logger

# A plain bool: the signal handler sets it, and a handler must not take a lock.
_shutting_down = False
# Each stop step with its name (None: unnamed), in the order registered.
_callbacks: list[tuple[str | None, Callable[[], object]]] = []
_callbacks_lock = threading.Lock()
# Held while the callbacks run, so the exit hook waits for a run the signal began rather than starting another.
_run_lock = threading.Lock()
_callbacks_ran = False
_exit_hook_registered = False
# The signal handler wakes the thread that runs the callbacks by writing a byte here: a write takes no lock.
_wake_fd: int | None = None


def request_shutdown() -> None:
    """Mark the process as stopping."""
    global _shutting_down
    _shutting_down = True


def is_shutting_down() -> bool:
    """Whether the process has begun stopping."""
    return _shutting_down


def on_shutdown(callback: Callable[[], object], *, name: str | None = None) -> None:
    """Run ``callback`` once when the process begins stopping: right after SIGTERM / SIGINT, else at exit.

    Not only at exit: s6 kills the container's processes 3 s after a stop's SIGTERM (``S6_KILL_GRACETIME``), and
    gunicorn kills its worker once open requests (a browser tab's websocket never ends) outlast its graceful timeout,
    so an exit hook alone may never run.
    The callbacks run in the order registered, each once, on a thread of their own (:func:`install_shutdown_hooks`).

    Args:
        callback: Takes no arguments; what it raises is logged.
        name: A step registered again under the same name takes the old one's place: an app made again in one
            process (the tests make hundreds) stops its own scheduler and nodes, not every app's before it.
    """
    global _exit_hook_registered
    with _callbacks_lock:
        at = next((i for i, (named, _) in enumerate(_callbacks) if name is not None and named == name), None)
        if at is None:
            _callbacks.append((name, callback))
        else:
            _callbacks[at] = (name, callback)
        if not _exit_hook_registered:
            atexit.register(run_shutdown_callbacks)
            _exit_hook_registered = True


def run_shutdown_callbacks() -> None:
    """Set the flag, then run the :func:`on_shutdown` callbacks unless they ran already; a second caller waits for
    the first one's run to finish."""
    global _callbacks_ran
    request_shutdown()
    with _run_lock:
        if _callbacks_ran:
            return
        _callbacks_ran = True
        with _callbacks_lock:
            callbacks = [callback for _, callback in _callbacks]
        for callback in callbacks:
            try:
                callback()
            except Exception:
                logger.opt(exception=True).warning("A step of the app's stop failed")


def _start_waker() -> int:
    """Start the thread that runs the callbacks once the signal handler writes to the returned pipe."""
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)

    def wait_for_signal() -> None:
        try:
            woken = os.read(read_fd, 1)
        except OSError:
            return
        finally:
            os.close(read_fd)
        if woken:  # b"" once the write end is closed: no signal came
            started = time.monotonic()
            run_shutdown_callbacks()
            logger.info("Stop steps finished in {:.1f} s", time.monotonic() - started)

    threading.Thread(target=wait_for_signal, name="shutdown-hooks", daemon=True).start()
    return write_fd


def install_shutdown_hooks() -> None:
    """Set the flag on SIGTERM / SIGINT, start the :func:`on_shutdown` callbacks, then run the handler already
    installed (e.g. gunicorn's); at exit, set the flag.

    Signal handlers can only be installed from the main thread; elsewhere only the exit hook is registered.
    """
    global _wake_fd
    atexit.register(request_shutdown)
    if threading.current_thread() is not threading.main_thread():
        return
    if _wake_fd is None:
        _wake_fd = _start_waker()
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous = signal.getsignal(sig)

        def handler(signum: int, frame: FrameType | None, previous=previous) -> None:
            request_shutdown()
            if _wake_fd is not None:
                try:
                    os.write(_wake_fd, b"\0")
                except OSError:
                    pass  # The pipe is full: its thread was woken already.
            if callable(previous):
                previous(signum, frame)
            elif previous == signal.SIG_DFL:
                signal.signal(signum, signal.SIG_DFL)
                signal.raise_signal(signum)

        try:
            signal.signal(sig, handler)
            # signal.signal() turns off syscall restarting; gunicorn keeps it on for SIGTERM.
            signal.siginterrupt(sig, False)
        except (ValueError, OSError) as exc:
            logger.debug("Could not install the shutdown handler for {}: {}", sig.name, exc)

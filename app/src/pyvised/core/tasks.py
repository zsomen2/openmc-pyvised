"""Run blocking calls (HTTP, docker CLI) off the GUI thread."""

import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from PySide6.QtCore import QObject, Signal

log = logging.getLogger(__name__)


class TaskRunner(QObject):
    """A thread pool whose results are delivered back in the GUI thread."""

    _finished = Signal(object, object)  # callback, value or exception

    def __init__(self, parent: QObject | None = None, max_workers: int = 4) -> None:
        super().__init__(parent)
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="pyvised")
        self._finished.connect(self._deliver)

    def submit(
        self,
        func: Callable[..., Any],
        *args: Any,
        on_result: Callable[[Any], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> Future:
        def job() -> None:
            try:
                value = func(*args)
            except Exception as exc:
                if on_error is None:
                    log.exception("background task %s failed", getattr(func, "__name__", func))
                self._finished.emit(on_error, exc)
            else:
                self._finished.emit(on_result, value)

        return self._pool.submit(job)

    def _deliver(self, callback: Callable[[Any], None] | None, payload: Any) -> None:
        if callback is not None:
            callback(payload)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

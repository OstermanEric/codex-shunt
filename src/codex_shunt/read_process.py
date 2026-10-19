"""Bounded, non-shell subprocesses used to resolve declarative reads."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path


def run_read_process(args: list[str], cwd: Path, limit: int, timeout: float = 5) -> bytes:
    """Never buffer unlimited search output or execute a supplied shell program."""
    expired = threading.Event()
    with subprocess.Popen(args, cwd=cwd, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        def expire() -> None:
            expired.set()
            try:
                process.kill()
            except OSError:
                pass

        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        timer.start()
        try:
            assert process.stdout is not None
            output = process.stdout.read(limit + 1)
            if len(output) > limit:
                process.kill()
                raise ValueError("read-output-limit")
            process.wait()
            if expired.is_set():
                raise ValueError("read-timeout")
            if process.returncode not in {0, 1}:
                raise ValueError("read-process-failed")
            return output
        finally:
            timer.cancel()


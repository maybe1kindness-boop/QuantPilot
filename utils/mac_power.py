"""Keep macOS awake while a long-running data download is active."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from typing import Iterator

logger = logging.getLogger(__name__)


@contextmanager
def keep_mac_awake(reason: str) -> Iterator[None]:
    """Prevent system idle sleep while allowing the display to turn off."""
    process = None
    if sys.platform == "darwin":
        executable = shutil.which("caffeinate") or "/usr/bin/caffeinate"
        try:
            process = subprocess.Popen(
                [executable, "-i", "-w", str(os.getpid())],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info("macOS 下载保活已启用: %s", reason)
        except (OSError, ValueError) as exc:
            logger.warning("无法启用 macOS 下载保活: %s", exc)

    try:
        yield
    finally:
        if process is not None:
            try:
                process.terminate()
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
            except OSError:
                pass
            logger.info("macOS 下载保活已释放: %s", reason)

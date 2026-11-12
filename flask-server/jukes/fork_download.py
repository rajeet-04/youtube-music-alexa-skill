"""Preload yt-dlp imports once, with a fresh killable process per extraction.

The forkserver is a clean interpreter, not a fork of Flask's threaded process.
It holds no job arguments, cookies, downloader objects or media connections.
"""
from __future__ import annotations

import io
import multiprocessing as mp
from multiprocessing.connection import Connection
import os
import subprocess
import sys
import threading

_context = mp.get_context('forkserver')
_lock = threading.Lock()
_warmed = False


def _warm():
    pass


def warm():
    global _warmed
    with _lock:
        if _warmed:
            return
        mp.set_forkserver_preload(['yt_dlp', 'yt_dlp.extractor.youtube'])
        child = _context.Process(target=_warm)
        child.start()
        child.join(15)
        if child.is_alive():
            child.kill()
            child.join()
            child.close()
            raise RuntimeError('extractor preload timed out')
        code = child.exitcode
        child.close()
        if code:
            raise RuntimeError('extractor preload failed')
        _warmed = True


def _run(arguments, output, errors):
    os.setsid()
    for connection, target in ((output, 1), (errors, 2)):
        os.dup2(connection.fileno(), target)
        connection.close()
    with open(os.devnull, 'rb') as null:
        os.dup2(null.fileno(), 0)
    # CLI download stdout is binary, diagnostics remain isolated on stderr.
    sys.stdout = io.TextIOWrapper(os.fdopen(1, 'wb', closefd=False), encoding='utf-8')
    sys.stderr = io.TextIOWrapper(os.fdopen(2, 'wb', closefd=False), encoding='utf-8')
    import yt_dlp
    yt_dlp.main(arguments)


class ForkDownload:
    """The small Popen surface used by Extractor; job CLI options are unchanged."""

    def __init__(self, command, **kwargs):
        if command[0] != 'yt-dlp':
            raise ValueError('only yt-dlp commands are supported')
        warm()
        output_read, output_write = os.pipe()
        error_read, error_write = os.pipe()
        self.stdout = os.fdopen(output_read, 'rb')
        self.stderr = os.fdopen(error_read, 'rb')
        # Connections reduce their descriptors during Process.start(), when the
        # forkserver owns descriptor transfer. Pre-created DupFd objects would
        # retain parent writers forever if a child failed before detaching them.
        output = Connection(output_write, readable=False, writable=True)
        errors = Connection(error_write, readable=False, writable=True)
        try:
            self._process = _context.Process(target=_run,
                args=(list(command[1:]), output, errors))
            self._process.start()
        except BaseException:
            self.stdout.close()
            self.stderr.close()
            raise
        finally:
            output.close()
            errors.close()
        self.pid = self._process.pid
        self.args = command

    @property
    def returncode(self):
        return self._process.exitcode

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self._process.join(timeout)
        if self._process.is_alive():
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.returncode

    def kill(self):
        self._process.kill()

    def close(self):
        self.stdout.close()
        self.stderr.close()
        if not self._process.is_alive():
            self._process.close()

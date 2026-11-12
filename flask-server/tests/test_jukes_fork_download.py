"""Real process tests for preloaded, isolated yt-dlp extraction children."""
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_preloaded_child_preserves_cli_output_and_exit_codes():
    from jukes.fork_download import ForkDownload
    child = ForkDownload(['yt-dlp', '--version'])
    assert child.stdout.read().strip()
    assert child.stderr.read() == b''
    assert child.wait(timeout=5) == 0
    assert child.poll() == 0
    child.close()
    failed = ForkDownload(['yt-dlp', '--jukes-invalid-option'])
    assert failed.stdout.read() == b''
    assert b'no such option' in failed.stderr.read()
    assert failed.wait(timeout=5) != 0
    failed.close()


def test_preloaded_child_streams_exact_binary_source_and_group_can_be_killed(tmp_path):
    from jukes.fork_download import ForkDownload
    source = tmp_path/'audio.mp3'
    # Generic extractor direct media: local file transport keeps this test offline.
    payload = b'ID3' + bytes(range(256))*65536
    source.write_bytes(payload)
    command = ['yt-dlp', '--enable-file-urls', '--quiet', '-o', '-', source.as_uri()]
    child = ForkDownload(command)
    assert child.stdout.read() == payload
    assert child.wait(timeout=5) == 0
    child.close()
    blocked = ForkDownload(command)
    # Child has filled its pipe and cannot finish without a reader.
    assert blocked.stdout.read(64)
    assert os.getpgid(blocked.pid) == blocked.pid
    with pytest.raises(subprocess.TimeoutExpired):
        blocked.wait(timeout=.05)
    os.killpg(blocked.pid, signal.SIGTERM)
    assert blocked.wait(timeout=5) < 0
    blocked.close()


def test_extractor_selects_preload_only_when_enabled(monkeypatch):
    from jukes.extractor import Extractor
    from jukes import fork_download
    warmed = []
    monkeypatch.setattr(fork_download, 'warm', lambda: warmed.append(True))
    monkeypatch.delenv('JUKES_EXTRACTOR_FORKSERVER', raising=False)
    assert Extractor()._popen is subprocess.Popen
    monkeypatch.setenv('JUKES_EXTRACTOR_FORKSERVER', '1')
    assert Extractor()._popen is fork_download.ForkDownload
    assert warmed == [True]
    supplied = lambda *args, **kwargs: None
    assert Extractor(process_factory=supplied)._popen is supplied


def test_startup_failure_does_not_retain_pipe_descriptors(monkeypatch):
    from jukes import fork_download
    fork_download.warm()
    class Failed:
        def start(self):
            raise OSError('process admission failed')
    monkeypatch.setattr(fork_download._context, 'Process', lambda **kwargs: Failed())
    before = len(os.listdir('/proc/self/fd'))
    for _ in range(5):
        with pytest.raises(OSError):
            fork_download.ForkDownload(['yt-dlp', '--version'])
    assert len(os.listdir('/proc/self/fd')) == before


def _stop_before_cli(arguments, output, errors):
    os.kill(os.getpid(), signal.SIGSTOP)


def test_child_killed_before_cli_closes_writers_and_delivers_eof(monkeypatch):
    import time
    from jukes import fork_download
    fork_download.warm()
    monkeypatch.setattr(fork_download, '_run', _stop_before_cli)
    before = len(os.listdir('/proc/self/fd'))
    child = fork_download.ForkDownload(['yt-dlp', '--version'])
    deadline = time.monotonic() + 5
    while '\nState:\tT' not in Path(f'/proc/{child.pid}/status').read_text():
        assert time.monotonic() < deadline
        time.sleep(.01)
    child.kill()
    assert child.wait(timeout=5) < 0
    assert child.stdout.read() == b''
    assert child.stderr.read() == b''
    child.close()
    assert len(os.listdir('/proc/self/fd')) == before

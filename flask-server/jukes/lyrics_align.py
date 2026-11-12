"""Find where a song recording sits inside another upload of it (numpy + ffmpeg).

An official video often carries the correct lyrics as hand-made subtitles, but it has a
different length (intro, outro). If the video contains the same recording, onsets of the
two line up at one constant offset everywhere; that offset maps the subtitles onto the
song. A different performance (live, remix) gives no consistent offset.

Rangeen showed two traps: a repeated chorus matches its other repeats, and a different
encoding (m4a vs opus) shifts loudness. So the search is limited to offsets that keep the
whole song inside the video, and the feature is multi-band spectral flux, energy-normalised.
"""

from __future__ import annotations

import subprocess

import numpy as np

RATE = 100         # feature frames per second (10 ms hop at 16 kHz)
WINDOW_S = 15      # length of each song excerpt searched for in the video
WINDOWS = 8        # excerpts spread over the song
AGREE_S = 0.1      # excerpts must agree on the offset within this
MIN_AGREEING = 6   # of WINDOWS
SLACK_S = 2        # the song may start up to this much before the video, or end after it


def envelope(path, timeout=60):
    """Spectral flux: summed per-band increases of log magnitude, z-normalised."""
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-ac', '1', '-ar', '16000',
                          '-f', 's16le', '-'], capture_output=True, timeout=timeout, check=True).stdout
    audio = np.frombuffer(raw[:len(raw) // 2 * 2], np.int16).astype(np.float32)
    size, hop = 512, 16000 // RATE
    if len(audio) < size * 2:
        return np.zeros(0)
    frames = np.lib.stride_tricks.sliding_window_view(audio, size)[::hop]
    spectrum = np.log1p(np.abs(np.fft.rfft(frames * np.hanning(size), axis=1)))
    flux = np.maximum(np.diff(spectrum, axis=0), 0).sum(axis=1)
    return (flux - flux.mean()) / (flux.std() or 1.0)


def offset(song, video):
    """Seconds to subtract from video times to get song times, or None if not the same recording."""
    win, slack = WINDOW_S * RATE, SLACK_S * RATE
    if len(song) < win * 2 or len(video) < len(song) - slack:  # the video must hold the whole song
        return None
    squares = np.concatenate([[0.0], np.cumsum(video ** 2)])
    energy = np.sqrt(squares[win:] - squares[:-win] + 1e-9)  # per video excerpt
    low, high = -slack, len(video) - len(song) + slack        # offsets keeping the song inside
    found = []
    for k in range(WINDOWS):
        start = int((len(song) - win) * (k + 0.5) / WINDOWS)
        score = np.correlate(video, song[start:start + win], 'valid') / energy
        first, last = max(start + low, 0), min(start + high, len(score) - 1)
        if first > last:
            return None
        found.append((first + int(score[first:last + 1].argmax()) - start) / RATE)
    median = float(np.median(found))
    agreeing = [f for f in found if abs(f - median) <= AGREE_S]
    return round(float(np.mean(agreeing)), 2) if len(agreeing) >= MIN_AGREEING else None

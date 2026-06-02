"""Audio capture — AT2035/PreSonus (primary), DJI Mic 2 (fallback), rolling buffer."""

import threading
from typing import Optional

import numpy as np
import sounddevice as sd


# Priority list for device detection — first match wins.
DEVICE_PRIORITY = [
    {'match': 'PreSonus', 'label': 'AT2035 via PreSonus Studio 26c'},
    {'match': 'Studio 26', 'label': 'AT2035 via PreSonus Studio 26c'},
    {'match': 'DJI',       'label': 'DJI Mic 2 USB Receiver'},
    {'match': 'Wireless Microphone', 'label': 'DJI Wireless Microphone'},
    {'match': 'CABLE',     'label': 'VB-Audio Virtual Cable (test mode)'},
]


def detect_audio_device() -> tuple:
    """
    Priority-based audio device detection. Returns (device_index, label, sample_rate).
    Raises RuntimeError if no suitable device found.
    """
    devices = sd.query_devices()
    for entry in DEVICE_PRIORITY:
        for idx, dev in enumerate(devices):
            if (entry['match'].lower() in dev['name'].lower()
                    and dev['max_input_channels'] > 0):
                return idx, entry['label'], int(dev['default_samplerate'])

    raise RuntimeError(
        "No suitable audio input device found.\n"
        "Run with --devices to list available devices."
    )


class AudioCapture:
    FFT_WINDOW_SECONDS = 0.5    # 500ms analysis window
    LUFS_WINDOW_SECONDS = 3.0  # 3s LUFS integration window

    def __init__(self, device_name_match: str = "", buffer_seconds: float = 3.0,
                 preferred_sample_rate: int = 48000,
                 forced_device_index: Optional[int] = None):
        self._match = device_name_match
        self._buffer_seconds = buffer_seconds
        self._preferred_sr = preferred_sample_rate
        self._forced_index = forced_device_index
        self._device_index: Optional[int] = None
        self._sample_rate: int = preferred_sample_rate
        self._stream: Optional[sd.InputStream] = None
        self._buffer: Optional[np.ndarray] = None
        self._lock = threading.Lock()
        self._write_pos = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def find_device(self) -> tuple:
        """Return (index, name, sample_rate) for best matching input device.

        When device_name_match is empty, uses priority detection (PreSonus > DJI > fallback).
        When set, matches that string directly.
        Raises RuntimeError if no match found.
        """
        if not self._match:
            try:
                idx, label, sr = detect_audio_device()
                return idx, label, sr
            except RuntimeError:
                pass

        devices = sd.query_devices()
        if self._match:
            matches = [
                (i, dev) for i, dev in enumerate(devices)
                if self._match.lower() in dev["name"].lower() and dev["max_input_channels"] > 0
            ]
            if matches:
                preferred = [(i, dev) for i, dev in matches
                             if int(dev["default_samplerate"]) == self._preferred_sr]
                chosen_i, chosen_dev = preferred[0] if preferred else matches[0]
                return chosen_i, chosen_dev["name"], int(chosen_dev["default_samplerate"])

        lines = [
            f"  [{i}] {d['name']}  — {int(d['default_samplerate'])}Hz, {d['max_input_channels']}ch in"
            for i, d in enumerate(devices) if d["max_input_channels"] > 0
        ]
        raise RuntimeError(
            f"No audio input device matching '{self._match or 'any known device'}' found.\n"
            "Available input devices:\n" + "\n".join(lines)
        )

    def list_devices(self) -> str:
        """Return a formatted string of all input devices, priority devices listed first."""
        devices = sd.query_devices()
        lines = ['Available audio input devices (priority devices marked):']
        shown = set()
        for entry in DEVICE_PRIORITY:
            for i, dev in enumerate(devices):
                if (entry['match'].lower() in dev['name'].lower()
                        and dev['max_input_channels'] > 0 and i not in shown):
                    lines.append(
                        f"  [{i}] {dev['name']:<40} -- {int(dev['default_samplerate'])}Hz, "
                        f"{dev['max_input_channels']}ch  <-- {entry['label']}"
                    )
                    shown.add(i)
        for i, dev in enumerate(devices):
            if dev['max_input_channels'] > 0 and i not in shown:
                lines.append(
                    f"  [{i}] {dev['name']:<40} -- {int(dev['default_samplerate'])}Hz, "
                    f"{dev['max_input_channels']}ch"
                )
        return "\n".join(lines)

    def start(self) -> None:
        """Find device (or use forced index) and start the audio stream as mono."""
        if self._forced_index is not None:
            dev = sd.query_devices(self._forced_index)
            self._device_index = self._forced_index
            self._sample_rate = int(dev["default_samplerate"])
        else:
            self._device_index, _, self._sample_rate = self.find_device()

        buf_frames = int(self._sample_rate * self._buffer_seconds)
        self._buffer = np.zeros(buf_frames, dtype=np.float32)
        self._write_pos = 0

        self._stream = sd.InputStream(
            device=self._device_index,
            channels=1,
            samplerate=self._sample_rate,
            dtype="float32",
            blocksize=int(self._sample_rate * 0.05),   # 50ms blocks
            callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def get_buffer(self) -> tuple:
        """Return a copy of the rolling buffer and the current sample rate."""
        with self._lock:
            if self._buffer is None:
                return np.zeros(0, dtype=np.float32), self._sample_rate
            pos = self._write_pos
            buf = np.concatenate([self._buffer[pos:], self._buffer[:pos]])
            return buf.copy(), self._sample_rate

    def get_analysis_window(self) -> np.ndarray:
        """Return the most recent FFT_WINDOW_SECONDS of audio (500ms)."""
        n = int(self.FFT_WINDOW_SECONDS * self._sample_rate)
        buf, _ = self.get_buffer()
        return buf[-n:] if len(buf) >= n else buf

    def get_lufs_window(self) -> np.ndarray:
        """Return the full 3-second LUFS integration buffer."""
        buf, _ = self.get_buffer()
        return buf

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def device_name(self) -> str:
        if self._device_index is None:
            return "not connected"
        return sd.query_devices(self._device_index)["name"]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _callback(self, indata: np.ndarray, frames: int, time, status) -> None:
        mono = indata[:, 0]
        with self._lock:
            buf_len = len(self._buffer)
            end = self._write_pos + frames
            if end <= buf_len:
                self._buffer[self._write_pos:end] = mono
            else:
                first = buf_len - self._write_pos
                self._buffer[self._write_pos:] = mono[:first]
                self._buffer[:end - buf_len] = mono[first:]
            self._write_pos = end % buf_len

"""The ALSA seam: the USB microphone and the speaker, as restartable streams.

Driven by `arecord`/`aplay` subprocesses rather than a Python binding, and that
is a deliberate choice, not a shortcut:

* **A USB device that disappears must not take the node with it.** Unplugging
  the webcam-and-mic mid-run kills the child process; the supervisor here sees
  the pipe close, reports unavailable, and retries. A PortAudio binding raises
  from inside a callback thread on another stack, which is markedly harder to
  recover from cleanly.
* **No new dependency.** `alsa-utils` is already on the Pi image; `sounddevice`
  would add PortAudio and a wheel to build for ARM.
* **The format is fixed at the process boundary.** Asking `arecord` for
  `S16_LE` mono at a stated rate makes ALSA do any conversion the device needs,
  so a mic that only does 48 kHz stereo still arrives as what /audio/in
  promises. Doing that conversion in Python would be code to get wrong.

Nothing in this module imports ROS. `mic_hw` and `speaker_hw` wrap it.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

BYTES_PER_SAMPLE = 2  # S16_LE, mono throughout (AudioChunk's contract)


@dataclass(frozen=True)
class AlsaDevice:
    """One ALSA card/device, as `arecord -l` / `aplay -l` report it."""

    card: int
    device: int
    name: str

    @property
    def plughw(self) -> str:
        """`plughw:` rather than `hw:`, so ALSA resamples and reformats for us.

        With bare `hw:` a mic that cannot do 16 kHz mono simply fails to open,
        which is the single most common way USB audio "does not work".
        """
        return f"plughw:{self.card},{self.device}"


_CARD_LINE = re.compile(r"^card (\d+): [^\[]*\[([^\]]+)\], device (\d+): ")


def _list(command: str) -> list[AlsaDevice]:
    if shutil.which(command) is None:
        return []
    try:
        out = subprocess.run(
            [command, "-l"], capture_output=True, text=True, timeout=5
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    found = []
    for line in out.splitlines():
        m = _CARD_LINE.match(line.strip())
        if m:
            found.append(AlsaDevice(card=int(m.group(1)), device=int(m.group(3)), name=m.group(2).strip()))
    return found


def capture_devices() -> list[AlsaDevice]:
    return _list("arecord")


def playback_devices() -> list[AlsaDevice]:
    return _list("aplay")


# HDMI audio is always present on a Pi and is never what anyone means by "the
# speaker" -- it is a monitor port with no monitor on it. Preferring anything
# else stops a USB speaker from silently losing to card 0.
_NOT_A_SPEAKER = ("vc4hdmi", "hdmi")


def default_capture() -> AlsaDevice | None:
    devices = capture_devices()
    return devices[0] if devices else None


def default_playback() -> AlsaDevice | None:
    devices = playback_devices()
    usb = [d for d in devices if not any(tag in d.name.lower() for tag in _NOT_A_SPEAKER)]
    return (usb or devices or [None])[0]


class MicCapture:
    """Mono S16_LE capture, restarted whenever the device goes away.

    `read()` returns b"" when no audio is available rather than blocking
    forever or raising -- a node polling a dead mic should log and carry on,
    because the fix (plug it back in) happens outside the process.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        device: str | None = None,
        chunk_ms: int = 100,
        retry_s: float = 2.0,
    ) -> None:
        self.sample_rate = sample_rate
        self.chunk_ms = chunk_ms
        self.retry_s = retry_s
        self._requested = device
        self._proc: subprocess.Popen | None = None
        self._next_try = 0.0
        self._lock = threading.Lock()
        self._error: str | None = None

    @property
    def chunk_bytes(self) -> int:
        return int(self.sample_rate * self.chunk_ms / 1000) * BYTES_PER_SAMPLE

    @property
    def error(self) -> str | None:
        return self._error

    def device_name(self) -> str | None:
        if self._requested:
            return self._requested
        found = default_capture()
        return found.plughw if found else None

    def available(self) -> tuple[bool, str]:
        if shutil.which("arecord") is None:
            return False, "arecord not installed (apt install alsa-utils)"
        if self.device_name() is None:
            return False, "no ALSA capture device -- is the USB microphone plugged in?"
        return True, "ok"

    def start(self) -> bool:
        with self._lock:
            return self._start_locked()

    def _start_locked(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        now = time.monotonic()
        if now < self._next_try:
            return False
        self._next_try = now + self.retry_s

        device = self.device_name()
        if device is None:
            self._error = "no ALSA capture device"
            return False
        try:
            self._proc = subprocess.Popen(
                [
                    "arecord", "-D", device,
                    "-f", "S16_LE", "-c", "1",
                    "-r", str(self.sample_rate),
                    "-t", "raw",
                    "--buffer-size", str(int(self.sample_rate * 0.5)),
                    "-q", "-",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            self._error = f"could not start arecord: {exc}"
            self._proc = None
            return False
        self._error = None
        log.info("microphone open on %s at %d Hz", device, self.sample_rate)
        return True

    def read(self) -> bytes:
        """One chunk, or b"" if the mic is not (yet) delivering."""
        with self._lock:
            if not self._start_locked():
                return b""
            proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            data = proc.stdout.read(self.chunk_bytes)
        except (OSError, ValueError):
            data = b""
        if not data:
            # The device went away mid-stream. Tear down so the next call
            # re-opens it rather than spinning on a closed pipe.
            self.stop()
            self._error = "capture stream ended -- device unplugged?"
            return b""
        return data

    def stop(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=1.0)
        except (subprocess.SubprocessError, OSError):
            try:
                proc.kill()
            except OSError:
                pass


class SpeakerPlayback:
    """Mono S16_LE playback through one long-lived `aplay`.

    One process for the whole session, not one per utterance: spawning `aplay`
    per reply adds ~100 ms of start-up before the first sample and clips the
    beginning of short answers.
    """

    def __init__(self, sample_rate: int = 22050, device: str | None = None) -> None:
        self.sample_rate = sample_rate
        self._requested = device
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._played_until = 0.0

    def device_name(self) -> str | None:
        if self._requested:
            return self._requested
        found = default_playback()
        return found.plughw if found else None

    def available(self) -> tuple[bool, str]:
        if shutil.which("aplay") is None:
            return False, "aplay not installed (apt install alsa-utils)"
        if self.device_name() is None:
            return False, "no ALSA playback device"
        return True, "ok"

    def _start_locked(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        device = self.device_name()
        if device is None:
            return False
        try:
            self._proc = subprocess.Popen(
                ["aplay", "-D", device, "-f", "S16_LE", "-c", "1",
                 "-r", str(self.sample_rate), "-t", "raw", "-q", "-"],
                stdin=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            log.error("could not start aplay: %s", exc)
            self._proc = None
            return False
        return True

    def play(self, pcm: bytes) -> None:
        with self._lock:
            if not self._start_locked():
                return
            proc = self._proc
            assert proc is not None and proc.stdin is not None
            try:
                proc.stdin.write(pcm)
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self._proc = None
                return
            # Track when this audio will actually finish leaving the speaker.
            # Writing to a pipe returns long before the sound is audible, and
            # the half-duplex gate needs the *audible* end, not the write.
            now = time.monotonic()
            duration = len(pcm) / (self.sample_rate * BYTES_PER_SAMPLE)
            self._played_until = max(self._played_until, now) + duration

    def busy_until(self) -> float:
        """Monotonic time the last audio written stops being audible."""
        return self._played_until

    def is_playing(self) -> bool:
        return time.monotonic() < self._played_until

    def stop(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
            self._played_until = 0.0
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=1.0)
        except (subprocess.SubprocessError, OSError):
            try:
                proc.kill()
            except OSError:
                pass

"""The wake word "NEO", from a Teachable Machine audio model, in pure numpy.

Teachable Machine exports an audio project as TensorFlow.js: `model.json`
(topology) plus `weights.bin`. Two things make it runnable on the Pi without
TensorFlow, TFJS or a browser:

* **The export is a whole model, not a transfer head.** TM's *image* projects
  ship a small head that needs a separately-hosted MobileNet base. Its audio
  projects inline the entire BrowserFFT speech-commands network -- four Conv2D
  blocks, a 2000-unit Dense, then the trained 2-class head -- 1.43 M parameters,
  all present in `weights.bin`. Nothing is fetched at runtime.
* **The architecture is four ops.** Conv2D/ReLU, MaxPool2D, Dense and softmax.
  `im2col` plus a matmul covers all of it, so the dependency is numpy, which
  the detector stack already pulls in. Installing TensorFlow on an ARM Pi to
  run 11 MFLOPs would be the tail wagging the dog.

The delicate part is not the network, it is the **spectrogram**. The model was
trained on frames produced by the Web Audio `AnalyserNode` in the browser, so
inference has to produce the same ones or the learned features land on the
wrong bins. `BrowserFft` below replicates that pipeline exactly; the details
that matter are documented on it.

What saves this from being fragile: speech-commands normalises each spectrogram
to zero mean and unit variance before inference. A constant scale factor on the
magnitudes is a constant *offset* in dB, and the mean subtraction removes it --
so the one part of the Web Audio spec that is genuinely awkward to match (how
it normalises FFT magnitudes) cannot affect the result. Window shape, FFT size,
hop and bin count all can, and those are matched exactly.
"""

from __future__ import annotations

import json
import logging
import math
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

# -- the BrowserFFT contract --------------------------------------------------
#
# These are not tunable. They are what tfjs-models/speech-commands v0.4 fed the
# network during training, read off the exported input shape [null, 43, 232, 1]
# and the library's own BrowserFftFeatureExtractor.

SAMPLE_RATE_HZ = 44100
"""What the browser's AudioContext ran at, so what the bins mean.

Bin k is k * 44100 / 2048 = 21.53 Hz. Audio at any other rate has to be
resampled to this before framing, or every learned feature is on the wrong bin.
"""

FFT_SIZE = 2048
"""`AnalyserNode.fftSize`. The library sets its own `fftSize = 1024` and then
assigns `analyser.fftSize = this.fftSize * 2`, which is where the factor of two
that is easy to miss comes from."""

HOP_SAMPLES = 1024
"""One frame every `fftSize / 2` samples -- 23.22 ms, the rate the library polls
the analyser at. Consecutive FFT windows therefore overlap by half."""

NUM_FRAMES = 43
COLUMN_TRUNCATE_LENGTH = 232
"""Only the first 232 of the analyser's 1024 bins are kept: 0-4995 Hz. Speech
lives there, and it is why 16 kHz capture loses nothing the model looks at."""

WINDOW_SAMPLES = (NUM_FRAMES - 1) * HOP_SAMPLES + FFT_SIZE
"""45056 samples, 1.0217 s -- one full spectrogram's worth of audio."""

DB_FLOOR = -120.0
"""Digital silence is 20*log10(0) = -inf, which poisons the mean and std that
normalisation is built from. Real mic noise never reaches it; a file of zeros
does, and a NaN spectrogram scores as confidently as a real one."""


def _blackman(n: int) -> np.ndarray:
    """The Web Audio spec's Blackman window, which is *not* numpy's.

    `np.blackman` uses the exact-symmetric form (divides by N-1); the Web Audio
    spec divides by N. On 2048 points the difference is small but systematic,
    and it is free to get right.
    """
    i = np.arange(n, dtype=np.float64)
    a0, a1, a2 = 0.42, 0.5, 0.08
    return (a0 - a1 * np.cos(2 * math.pi * i / n) + a2 * np.cos(4 * math.pi * i / n)).astype(
        np.float32
    )


class BrowserFft:
    """Audio -> the [43, 232] spectrogram the model was trained on.

    Mirrors `AnalyserNode.getFloatFrequencyData`: Blackman window, 2048-point
    FFT, magnitude, then decibels. Temporal smoothing is skipped because the
    library sets `smoothingTimeConstant = 0`.
    """

    def __init__(self) -> None:
        self._window = _blackman(FFT_SIZE)

    def spectrogram(self, samples: np.ndarray) -> np.ndarray:
        """`samples` is float32 mono at 44100 Hz, exactly WINDOW_SAMPLES long."""
        if samples.shape[0] != WINDOW_SAMPLES:
            raise ValueError(f"need {WINDOW_SAMPLES} samples at {SAMPLE_RATE_HZ} Hz, got {samples.shape[0]}")

        # One row per frame, overlapping by half, without copying.
        frames = np.lib.stride_tricks.as_strided(
            samples,
            shape=(NUM_FRAMES, FFT_SIZE),
            strides=(samples.strides[0] * HOP_SAMPLES, samples.strides[0]),
            writeable=False,
        )
        spectra = np.fft.rfft(frames * self._window, n=FFT_SIZE, axis=1)
        magnitude = np.abs(spectra[:, :COLUMN_TRUNCATE_LENGTH]) / FFT_SIZE

        # dB, floored rather than clipped to zero: log10(0) is -inf, and one
        # -inf makes the whole normalisation NaN.
        db = 20.0 * np.log10(np.maximum(magnitude, 1e-20))
        return np.maximum(db, DB_FLOOR).astype(np.float32)

    @staticmethod
    def normalize(spectrogram: np.ndarray) -> np.ndarray:
        """speech-commands' `normalize`: (x - mean) / std over the whole frame.

        This is what makes the dB offset above unimportant, and it is also why
        a silent room and a loud one score comparably.
        """
        std = float(spectrogram.std())
        if std < 1e-6:
            # Perfectly flat input carries no information; hand back zeros
            # rather than dividing by ~0 and amplifying float noise into
            # something the network will happily classify.
            return np.zeros_like(spectrogram)
        return (spectrogram - float(spectrogram.mean())) / std


def resample_to_44100(pcm: np.ndarray, from_rate: int) -> np.ndarray:
    """Linear resampling to the rate the bins are defined at.

    Linear interpolation is enough *here* specifically because the model reads
    only bins 0-231 (0-4995 Hz). Upsampling images the source spectrum around
    multiples of `from_rate`, which for any rate at or above 16 kHz lands well
    above 5 kHz -- outside every bin the network sees.
    """
    if from_rate == SAMPLE_RATE_HZ:
        return pcm.astype(np.float32, copy=False)
    duration = pcm.shape[0] / float(from_rate)
    target_n = int(round(duration * SAMPLE_RATE_HZ))
    if target_n <= 1 or pcm.shape[0] <= 1:
        return np.zeros(max(target_n, 0), dtype=np.float32)
    src_x = np.arange(pcm.shape[0], dtype=np.float64) / from_rate
    dst_x = np.arange(target_n, dtype=np.float64) / SAMPLE_RATE_HZ
    return np.interp(dst_x, src_x, pcm).astype(np.float32)


# -- the network --------------------------------------------------------------


def _conv2d(x: np.ndarray, kernel: np.ndarray, bias: np.ndarray) -> np.ndarray:
    """Valid-padding 2-D convolution, channels-last, via im2col + tensordot.

    `kernel` is [kh, kw, in_ch, out_ch], the layout TFJS and Keras both store.
    """
    h, w, _ = x.shape
    kh, kw, in_ch, out_ch = kernel.shape
    out_h, out_w = h - kh + 1, w - kw + 1
    sh, sw, sc = x.strides
    patches = np.lib.stride_tricks.as_strided(
        x, shape=(out_h, out_w, kh, kw, in_ch), strides=(sh, sw, sh, sw, sc), writeable=False
    )
    out = np.tensordot(patches, kernel, axes=([2, 3, 4], [0, 1, 2]))
    return out + bias


def _max_pool2d(x: np.ndarray, pool: tuple[int, int], strides: tuple[int, int]) -> np.ndarray:
    h, w, c = x.shape
    ph, pw = pool
    sh_, sw_ = strides
    out_h = (h - ph) // sh_ + 1
    out_w = (w - pw) // sw_ + 1
    sh, sw, sc = x.strides
    windows = np.lib.stride_tricks.as_strided(
        x,
        shape=(out_h, out_w, ph, pw, c),
        strides=(sh * sh_, sw * sw_, sh, sw, sc),
        writeable=False,
    )
    return windows.max(axis=(2, 3))


def _softmax(x: np.ndarray) -> np.ndarray:
    shifted = x - x.max()
    exp = np.exp(shifted)
    return exp / exp.sum()


@dataclass(frozen=True)
class WakeWordLabels:
    """What the trained classes mean.

    Teachable Machine names the first class "Background Noise" and leaves the
    rest as "Class 2", "Class 3"... unless they are renamed in the UI. The
    wake class is therefore identified by *position* -- anything that is not
    the background class -- rather than by a name that is usually a placeholder.
    """

    names: tuple[str, ...]
    background_index: int = 0

    @property
    def wake_index(self) -> int:
        for i in range(len(self.names)):
            if i != self.background_index:
                return i
        raise ValueError("the model has no class besides background noise")


class WakeWordModel:
    """The exported network, forward pass only."""

    def __init__(self, weights: dict[str, np.ndarray], labels: WakeWordLabels) -> None:
        self.labels = labels
        self._w = weights
        missing = [
            name
            for name in (
                "conv2d_1/kernel", "conv2d_1/bias",
                "conv2d_2/kernel", "conv2d_2/bias",
                "conv2d_3/kernel", "conv2d_3/bias",
                "conv2d_4/kernel", "conv2d_4/bias",
                "dense_1/kernel", "dense_1/bias",
                "NewHeadDense/kernel", "NewHeadDense/bias",
            )
            if name not in weights
        ]
        if missing:
            raise ValueError(
                f"the wake word model is missing {', '.join(missing)}. This loader expects a "
                f"Teachable Machine *audio* export (tfjsSpeechCommandsVersion in metadata.json); "
                f"an image or pose export has a different architecture entirely."
            )

    def predict(self, spectrogram: np.ndarray) -> np.ndarray:
        """[43, 232] normalised spectrogram -> class probabilities."""
        x = spectrogram[:, :, np.newaxis]
        w = self._w

        x = np.maximum(_conv2d(x, w["conv2d_1/kernel"], w["conv2d_1/bias"]), 0.0)
        x = _max_pool2d(x, (2, 2), (2, 2))
        x = np.maximum(_conv2d(x, w["conv2d_2/kernel"], w["conv2d_2/bias"]), 0.0)
        x = _max_pool2d(x, (2, 2), (2, 2))
        x = np.maximum(_conv2d(x, w["conv2d_3/kernel"], w["conv2d_3/bias"]), 0.0)
        x = _max_pool2d(x, (2, 2), (2, 2))
        x = np.maximum(_conv2d(x, w["conv2d_4/kernel"], w["conv2d_4/bias"]), 0.0)
        # The last block strides 1 vertically: only two rows survive, and
        # halving them again would throw away half the time axis.
        x = _max_pool2d(x, (2, 2), (1, 2))

        flat = np.ascontiguousarray(x).reshape(-1)          # Keras Flatten is C-order
        # Dropout is identity at inference.
        hidden = np.maximum(flat @ w["dense_1/kernel"] + w["dense_1/bias"], 0.0)
        logits = hidden @ w["NewHeadDense/kernel"] + w["NewHeadDense/bias"]
        return _softmax(logits)


def _read_export(path: Path) -> tuple[dict[str, Any], bytes]:
    """Accept the .zip Teachable Machine hands you, or an unpacked directory.

    Taking the zip as-is matters: it is what lands in Downloads, and asking
    someone to unpack it correctly is a step that can be got wrong silently.
    """
    if path.is_dir():
        return json.loads((path / "model.json").read_text()), (path / "weights.bin").read_bytes()
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            model_name = next((n for n in names if n.endswith("model.json")), None)
            weights_name = next((n for n in names if n.endswith("weights.bin")), None)
            if not model_name or not weights_name:
                raise ValueError(
                    f"{path} is not a Teachable Machine export: it has no model.json/weights.bin "
                    f"(found {', '.join(names[:6])})"
                )
            return json.loads(z.read(model_name)), z.read(weights_name)
    raise ValueError(f"{path} is neither a .zip nor a directory")


def load_model(path: str | Path) -> WakeWordModel:
    """Load a Teachable Machine audio export.

    The weights are one flat little-endian float32 blob; the manifest gives the
    order and shapes, and nothing else in the file says where one tensor ends.
    A truncated download therefore reads as a shape error here rather than as
    silently wrong predictions, which is the point of checking the total.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"no wake word model at {path}. Export the Teachable Machine audio project as "
            f"TensorFlow.js and point audio.wakeword.model_path at the .zip."
        )
    topology, blob = _read_export(path)

    weights: dict[str, np.ndarray] = {}
    offset = 0
    for group in topology.get("weightsManifest", []):
        for spec in group.get("weights", []):
            shape = tuple(spec["shape"])
            count = int(np.prod(shape)) if shape else 1
            end = offset + count * 4
            if end > len(blob):
                raise ValueError(
                    f"{path}: weights.bin is {len(blob)} bytes but the manifest describes at least "
                    f"{end}. The export is truncated -- download it again."
                )
            weights[spec["name"]] = (
                np.frombuffer(blob, dtype="<f4", count=count, offset=offset).reshape(shape).astype(np.float32)
            )
            offset = end
    if offset != len(blob):
        log.warning("%s: %d trailing bytes in weights.bin the manifest does not claim", path, len(blob) - offset)

    labels = WakeWordLabels(names=tuple(_labels_from(path)))
    return WakeWordModel(weights, labels)


def _labels_from(path: Path) -> list[str]:
    try:
        if path.is_dir():
            meta = json.loads((path / "metadata.json").read_text())
        else:
            with zipfile.ZipFile(path) as z:
                name = next(n for n in z.namelist() if n.endswith("metadata.json"))
                meta = json.loads(z.read(name))
        return list(meta.get("wordLabels") or meta.get("labels") or ["Background Noise", "Wake"])
    except Exception:  # noqa: BLE001 - labels are cosmetic; position is what matters
        return ["Background Noise", "Wake"]


# -- streaming detection ------------------------------------------------------


@dataclass
class WakeWordConfig:
    """Tuning. Only `threshold` normally needs touching."""

    model_path: str = ""
    threshold: float = 0.85
    """Probability the wake class must reach. High by default on purpose: a
    false accept opens the mic and sends the room's speech to the model host,
    which is a worse failure than having to say "NEO" twice."""

    threshold_with_person: float = 0.70
    """Relaxed while perception reports someone in frame (plan 5.2 cross-modal
    gating). Someone standing at the desk saying something that half-matches is
    far more likely to be addressing the robot than the same sound in an empty
    room, and `WakeEvent.threshold_applied` records which one was used."""

    consecutive_hits: int = 2
    """Windows that must clear the threshold back to back. The windows overlap
    by ~85%, so a real utterance clears several in a row while an impulse --
    a door, a cough, a chair -- clears at most one."""

    refractory_s: float = 2.0
    """Silence after firing. Without it one "NEO" fires on every overlapping
    window that still contains it, for a full second."""

    hop_frames: int = 6
    """Spectrogram frames between evaluations: 6 x 23.22 ms = 139 ms, ~7 Hz.
    The window is a whole second, so the phrase is well inside several of them;
    this trades latency against the cores YOLO wants."""

    def hop_samples_at(self, sample_rate: int) -> int:
        return max(1, int(round(self.hop_frames * HOP_SAMPLES * sample_rate / SAMPLE_RATE_HZ)))


@dataclass(frozen=True)
class Detection:
    fired: bool
    score: float
    threshold_applied: float
    person_present: bool


class WakeWordDetector:
    """Streaming wake word over a rolling one-second window.

    Feed it PCM as it arrives at whatever rate the mic runs at; it buffers,
    resamples once per evaluation, and reports. It is deliberately *not* a
    node: the panel drives this same class against a browser mic.
    """

    def __init__(
        self,
        model: WakeWordModel,
        config: WakeWordConfig | None = None,
        sample_rate: int = 16000,
    ) -> None:
        self.config = config or WakeWordConfig()
        self.model = model
        self.sample_rate = sample_rate
        self._fft = BrowserFft()
        # A second of audio at the *input* rate, plus room for one hop.
        self._need = int(math.ceil(WINDOW_SAMPLES * sample_rate / SAMPLE_RATE_HZ)) + 2
        self._buffer = np.zeros(0, dtype=np.float32)
        self._since_eval = 0
        self._hits = 0
        self._muted_until = 0.0
        self._last_score = 0.0

    @property
    def last_score(self) -> float:
        """The most recent wake-class probability, for the panel's meter."""
        return self._last_score

    def reset(self) -> None:
        self._buffer = np.zeros(0, dtype=np.float32)
        self._since_eval = 0
        self._hits = 0

    def mute_until(self, when: float) -> None:
        """Deafen the detector until `when`.

        Neo's own voice must never wake it. The dialog node holds the mic muted
        while a reply plays (half-duplex, CLAUDE.md); this is the same idea one
        layer down, so a stray chunk that arrives late cannot fire it either.
        """
        self._muted_until = max(self._muted_until, when)

    def accept(
        self, pcm: bytes | np.ndarray, now: float, *, person_present: bool = False
    ) -> Detection:
        """Take a chunk of mono PCM; report whether the wake word just fired."""
        if isinstance(pcm, bytes):
            samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        else:
            samples = np.asarray(pcm, dtype=np.float32)

        self._buffer = np.concatenate([self._buffer, samples])
        if self._buffer.shape[0] > self._need:
            self._buffer = self._buffer[-self._need :]
        self._since_eval += samples.shape[0]

        threshold = (
            self.config.threshold_with_person if person_present else self.config.threshold
        )
        quiet = Detection(False, self._last_score, threshold, person_present)

        if now < self._muted_until:
            # Still drop the audio into the buffer above, so the window is warm
            # the moment the mute lifts rather than needing another full second.
            self._hits = 0
            return quiet
        if self._buffer.shape[0] < self._need - 2:
            return quiet
        if self._since_eval < self.config.hop_samples_at(self.sample_rate):
            return quiet
        self._since_eval = 0

        window = resample_to_44100(self._buffer, self.sample_rate)
        if window.shape[0] < WINDOW_SAMPLES:
            return quiet
        spectrogram = self._fft.spectrogram(np.ascontiguousarray(window[-WINDOW_SAMPLES:]))
        probs = self.model.predict(BrowserFft.normalize(spectrogram))
        score = float(probs[self.model.labels.wake_index])
        self._last_score = score

        if score < threshold:
            self._hits = 0
            return Detection(False, score, threshold, person_present)

        self._hits += 1
        if self._hits < self.config.consecutive_hits:
            return Detection(False, score, threshold, person_present)

        self._hits = 0
        self._muted_until = now + self.config.refractory_s
        self.reset()
        return Detection(True, score, threshold, person_present)


def availability(config: WakeWordConfig) -> tuple[bool, str]:
    """Whether the wake word can run, and if not, what to do about it.

    Same shape as `intelligence.asr.availability` -- the panel shows the reason
    rather than a bare "unavailable", because every cause here has a different
    fix and they are not guessable from the outside.
    """
    if not config.model_path:
        return False, "no model configured (set audio.wakeword.model_path)"
    path = Path(config.model_path)
    if not path.exists():
        return False, f"model not found at {path}"
    try:
        load_model(path)
    except Exception as exc:  # noqa: BLE001 - the reason is the whole point
        return False, str(exc)
    return True, "ok"

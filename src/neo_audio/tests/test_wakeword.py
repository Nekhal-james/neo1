"""The wake word: the numpy network, the BrowserFFT front end, and detection."""

from __future__ import annotations

import zipfile

import numpy as np
import pytest

from neo_audio import wakeword as ww
from neo_audio.wakeword import (
    BrowserFft,
    WakeWordConfig,
    WakeWordDetector,
    WakeWordLabels,
    availability,
    load_model,
    resample_to_44100,
)

from .conftest import SHAPES, make_export

# -- the four ops, against loops nobody could get wrong --------------------------


def _naive_conv2d(x, k, b):
    h, w, _ = x.shape
    kh, kw, _, cout = k.shape
    out = np.zeros((h - kh + 1, w - kw + 1, cout))
    for i in range(h - kh + 1):
        for j in range(w - kw + 1):
            patch = x[i : i + kh, j : j + kw, :]
            for o in range(cout):
                out[i, j, o] = np.sum(patch * k[:, :, :, o]) + b[o]
    return out


def _naive_pool(x, pool, strides):
    h, w, c = x.shape
    out_h, out_w = (h - pool[0]) // strides[0] + 1, (w - pool[1]) // strides[1] + 1
    out = np.zeros((out_h, out_w, c))
    for i in range(out_h):
        for j in range(out_w):
            r, s = i * strides[0], j * strides[1]
            out[i, j] = x[r : r + pool[0], s : s + pool[1]].max(axis=(0, 1))
    return out


def test_conv2d_matches_a_naive_loop():
    rng = np.random.default_rng(1)
    x = rng.standard_normal((6, 9, 3)).astype(np.float32)
    k = rng.standard_normal((2, 4, 3, 5)).astype(np.float32)
    b = rng.standard_normal(5).astype(np.float32)
    np.testing.assert_allclose(ww._conv2d(x, k, b), _naive_conv2d(x, k, b), rtol=1e-4, atol=1e-4)


@pytest.mark.parametrize("strides", [(2, 2), (1, 2)])
def test_max_pool_matches_a_naive_loop(strides):
    """(1, 2) is the model's last pool, the one that keeps both time rows."""
    x = np.random.default_rng(2).standard_normal((5, 11, 4)).astype(np.float32)
    np.testing.assert_allclose(ww._max_pool2d(x, (2, 2), strides), _naive_pool(x, (2, 2), strides))


def test_the_window_is_web_audios_blackman_not_numpys():
    w = ww._blackman(8)
    assert w[0] == pytest.approx(0.0, abs=1e-6)
    assert w[4] == pytest.approx(1.0, abs=1e-6), "divides by N, so the peak is at N/2"
    assert not np.allclose(w, np.blackman(8))


# -- the spectrogram -------------------------------------------------------------


def _tone(freq, n, rate=ww.SAMPLE_RATE_HZ, amp=0.3):
    return (amp * np.sin(2 * np.pi * freq * np.arange(n) / rate)).astype(np.float32)


def test_a_tone_lands_on_its_bin():
    spec = BrowserFft().spectrogram(_tone(1000, ww.WINDOW_SAMPLES))
    assert spec.shape == (ww.NUM_FRAMES, ww.COLUMN_TRUNCATE_LENGTH) == (43, 232)
    assert int(np.argmax(spec.mean(axis=0))) == round(1000 * ww.FFT_SIZE / ww.SAMPLE_RATE_HZ)


def test_the_wrong_amount_of_audio_is_refused():
    with pytest.raises(ValueError, match="45056"):
        BrowserFft().spectrogram(np.zeros(1000, np.float32))


def test_silence_normalises_to_zeros_rather_than_nan():
    norm = BrowserFft.normalize(BrowserFft().spectrogram(np.zeros(ww.WINDOW_SAMPLES, np.float32)))
    assert np.isfinite(norm).all() and not norm.any()


def test_a_louder_mic_produces_the_same_normalised_input():
    """Gain is a constant dB offset, and the per-spectrogram mean removes it --
    which is why matching Web Audio's magnitude scaling does not matter."""
    rng = np.random.default_rng(3)
    signal = (rng.standard_normal(ww.WINDOW_SAMPLES) * 0.02).astype(np.float32) + _tone(700, ww.WINDOW_SAMPLES)
    fft = BrowserFft()
    np.testing.assert_allclose(
        fft.normalize(fft.spectrogram(signal)), fft.normalize(fft.spectrogram(signal * 4)), atol=1e-3
    )


def test_16_khz_capture_keeps_every_bin_the_model_reads():
    up = resample_to_44100(_tone(1000, 17600, rate=16000), 16000)
    assert abs(up.shape[0] - 48510) <= 1
    spec = BrowserFft().spectrogram(up[: ww.WINDOW_SAMPLES])
    assert int(np.argmax(spec.mean(axis=0))) == round(1000 * ww.FFT_SIZE / ww.SAMPLE_RATE_HZ)


def test_audio_already_at_44_1_khz_is_not_resampled():
    pcm = _tone(440, 1000)
    assert resample_to_44100(pcm, ww.SAMPLE_RATE_HZ) is pcm


# -- loading an export ---------------------------------------------------------


def test_a_teachable_machine_zip_loads_and_runs_every_layer(tm_zip):
    model = load_model(tm_zip)
    probs = model.predict(np.zeros((43, 232), np.float32))
    assert probs.shape == (2,)
    assert float(probs.sum()) == pytest.approx(1.0, abs=1e-5)
    assert model.labels.names == ("Background Noise", "Class 2")
    assert model.labels.wake_index == 1


def test_an_unpacked_export_is_the_same_model(tm_zip, tmp_path):
    with zipfile.ZipFile(tm_zip) as z:
        z.extractall(tmp_path / "export")
    spec = np.random.default_rng(4).standard_normal((43, 232)).astype(np.float32)
    np.testing.assert_allclose(load_model(tm_zip).predict(spec), load_model(tmp_path / "export").predict(spec))


def test_a_truncated_download_says_so(tmp_path):
    with pytest.raises(ValueError, match="truncated"):
        load_model(make_export(tmp_path / "half.zip", truncate=True))


def test_an_export_of_another_kind_is_refused_with_the_reason(tmp_path):
    image_like = [s for s in SHAPES if not s[0].startswith("conv2d")]
    with pytest.raises(ValueError, match="audio"):
        load_model(make_export(tmp_path / "image.zip", shapes=image_like))


def test_a_missing_model_is_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError, match="Teachable Machine"):
        load_model(tmp_path / "nope.zip")


def test_availability_names_what_to_fix(tmp_path, tm_zip):
    assert availability(WakeWordConfig())[1].startswith("no model configured")
    assert "not found" in availability(WakeWordConfig(model_path=str(tmp_path / "x.zip")))[1]
    assert availability(WakeWordConfig(model_path=str(tm_zip))) == (True, "ok")


def test_the_wake_class_is_found_by_position_not_by_its_placeholder_name():
    assert WakeWordLabels(("Background Noise", "Class 2")).wake_index == 1
    assert WakeWordLabels(("neo", "Background Noise"), background_index=1).wake_index == 0
    with pytest.raises(ValueError):
        WakeWordLabels(("Background Noise",)).wake_index


# -- streaming detection ------------------------------------------------------


class ScriptedModel:
    """Returns a scripted wake score per window instead of running a network."""

    def __init__(self, score):
        self.score = score
        self.labels = WakeWordLabels(("Background Noise", "Neo"))
        self.windows = 0

    def predict(self, _spectrogram):
        self.windows += 1
        s = self.score(self.windows) if callable(self.score) else self.score
        return np.array([1.0 - s, s], np.float32)


def _run(detector, seconds, *, start=0.0, person=False, chunk_s=0.1, rate=16000):
    chunk = (np.random.default_rng(5).standard_normal(int(rate * chunk_s)) * 300).astype("<i2").tobytes()
    fired, t = [], start
    for _ in range(int(round(seconds / chunk_s))):
        if detector.accept(chunk, t, person_present=person).fired:
            fired.append(t)
        t += chunk_s
    return fired, t


def test_the_hop_is_six_browser_frames_at_whatever_rate_the_mic_runs():
    assert WakeWordConfig().hop_samples_at(16000) == 2229
    assert WakeWordConfig().hop_samples_at(44100) == 6144


def test_one_window_over_the_threshold_is_not_enough():
    detector = WakeWordDetector(ScriptedModel(lambda n: 0.95 if n == 1 else 0.1))
    fired, _ = _run(detector, 3.0)
    assert fired == []


def test_an_utterance_fires_once_and_then_the_detector_rests():
    detector = WakeWordDetector(ScriptedModel(0.95), WakeWordConfig(refractory_s=2.0))
    fired, _ = _run(detector, 3.0)
    assert len(fired) == 1


def test_a_person_in_frame_relaxes_the_threshold_and_says_so():
    config = WakeWordConfig(threshold=0.85, threshold_with_person=0.70)
    assert _run(WakeWordDetector(ScriptedModel(0.75), config), 3.0)[0] == []

    detector = WakeWordDetector(ScriptedModel(0.75), config)
    results = []
    chunk = b"\x10\x00" * 1600
    for i in range(30):
        results.append(detector.accept(chunk, i * 0.1, person_present=True))
    fired = [r for r in results if r.fired]
    assert len(fired) == 1
    assert fired[0].threshold_applied == pytest.approx(0.70)
    assert fired[0].person_present


def test_a_muted_detector_never_fires_but_is_warm_when_the_mute_lifts():
    detector = WakeWordDetector(ScriptedModel(0.99))
    detector.mute_until(3.0)
    fired, t = _run(detector, 3.0)
    assert fired == []
    fired, _ = _run(detector, 1.0, start=t)
    assert fired and fired[0] < t + 0.6, "a full second of audio was already buffered"


def test_the_real_network_runs_end_to_end_on_live_chunks(tm_zip):
    detector = WakeWordDetector(load_model(tm_zip), sample_rate=16000)
    _run(detector, 2.0)
    assert 0.0 < detector.last_score <= 1.0

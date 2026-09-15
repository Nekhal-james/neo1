"""`neo-audio-check` -- prove the audio path works, without ROS.

The counterpart to `neo-servo-check`. Everything here runs against the same
cores the nodes wrap, so a problem found with this tool is a problem in what
the robot will actually run, and a tool that passes means the nodes have
nothing left to discover.

    neo-audio-check                 what is plugged in, and is the model loadable
    neo-audio-check --listen        live wake word scores -- how you set a threshold
    neo-audio-check --record 3      record and report levels, to check gain
    neo-audio-check --say "hello"   synthesise and play through the speaker
    neo-audio-check --transcribe    say something; see what Vosk heard

`--listen` is the one that matters. A wake word threshold cannot be chosen from
a config file: it depends on the room, the mic and the voice that trained the
model, and the only way to pick one is to watch the score while saying the word
and while not saying it.
"""

from __future__ import annotations

import argparse
import sys
import time

from ..config import Config
from ..devices import MicCapture, SpeakerPlayback, capture_devices, playback_devices


def _report_devices() -> bool:
    capture = capture_devices()
    playback = playback_devices()

    print("Capture devices:")
    if not capture:
        print("      none. `lsusb` should list the webcam/mic; if it does not, the")
        print("      device is not enumerating -- try another port, and prefer a")
        print("      powered hub for anything drawing more than a little current.")
    for device in capture:
        print(f"      {device.plughw:<16} {device.name}")

    print("Playback devices:")
    for device in playback:
        note = "  (HDMI -- not a speaker unless a monitor is attached)" if "hdmi" in device.name.lower() else ""
        print(f"      {device.plughw:<16} {device.name}{note}")
    if not playback:
        print("      none.")
    return bool(capture)


def _report_wakeword(config: Config) -> bool:
    from ..wakeword import availability, load_model

    path = config.wakeword_model_path
    if path is None:
        print("Wake word: not configured.")
        print("      Set audio.wakeword.model_path in config/audio.local.yaml to the")
        print("      Teachable Machine export (.zip). It does not need unpacking.")
        return False

    ok, why = availability(config.wakeword)
    if not ok:
        print(f"Wake word: FAIL  {why}")
        return False

    model = load_model(path)
    wake = model.labels.names[model.labels.wake_index]
    print(f"Wake word: ok    {path.name}")
    print(f"      classes: {', '.join(model.labels.names)}  (wake = {wake!r})")
    print(f"      threshold {config.wakeword.threshold} "
          f"({config.wakeword.threshold_with_person} with a person in frame), "
          f"{config.wakeword.consecutive_hits} hits in a row")
    return True


def _report_speech(config: Config) -> None:
    try:
        from intelligence.asr import availability as asr_availability
        from intelligence.config import Config as IntelligenceConfig
        from intelligence.tts import availability as tts_availability
    except ImportError as exc:
        print(f"Speech: unavailable -- {exc}")
        return

    cfg = IntelligenceConfig.load()
    asr = asr_availability(cfg)
    tts = tts_availability(cfg)
    print(f"Speech to text: {'ok' if asr.ok else 'FAIL  ' + asr.reason}")
    print(f"Text to speech: {'ok' if tts.ok else 'FAIL  ' + tts.reason}")


def _listen(config: Config, seconds: float) -> int:
    """Live wake word scores. This is how a threshold gets chosen."""
    from ..wakeword import WakeWordDetector, load_model

    path = config.wakeword_model_path
    if path is None:
        print("no wake word model configured -- see `neo-audio-check` with no arguments")
        return 1

    detector = WakeWordDetector(load_model(path), config.wakeword, sample_rate=config.mic.sample_rate)
    mic = MicCapture(
        sample_rate=config.mic.sample_rate,
        device=config.mic.device or None,
        chunk_ms=config.mic.chunk_ms,
    )
    ok, why = mic.available()
    if not ok:
        print(f"cannot listen: {why}")
        return 1

    print(f"Listening on {mic.device_name()} for {seconds:g}s. Say the wake word a few times.")
    print("A bar is one window's score; FIRED is the word being accepted.\n")
    deadline = time.monotonic() + seconds
    peak = 0.0
    fired = 0
    try:
        while time.monotonic() < deadline:
            data = mic.read()
            if not data:
                continue
            now = time.monotonic()
            result = detector.accept(data, now)
            score = detector.last_score
            if score == 0.0:
                continue
            peak = max(peak, score)
            bar = "#" * int(score * 50)
            mark = "  <-- FIRED" if result.fired else ""
            print(f"  {score:5.3f} |{bar:<50}|{mark}")
            if result.fired:
                fired += 1
    except KeyboardInterrupt:
        print()
    finally:
        mic.stop()

    print(f"\nPeak score {peak:.3f}; fired {fired} time(s).")
    if peak < 0.5:
        print("The model never came close. Check the mic is the right device and")
        print("that --record shows real levels; a silent mic scores ~0.5 on nothing.")
    elif fired == 0:
        print(f"Never reached {config.wakeword.threshold}. If the peaks above line up with")
        print("you saying the word, lower audio.wakeword.threshold to just under the peak.")
    return 0


def _record(config: Config, seconds: float) -> int:
    """Levels, so "the mic is muted" and "the model is wrong" stay separable."""
    import numpy as np

    mic = MicCapture(
        sample_rate=config.mic.sample_rate,
        device=config.mic.device or None,
        chunk_ms=config.mic.chunk_ms,
    )
    ok, why = mic.available()
    if not ok:
        print(f"cannot record: {why}")
        return 1

    print(f"Recording {seconds:g}s from {mic.device_name()} -- speak normally.")
    deadline = time.monotonic() + seconds
    peak = 0.0
    total = 0
    try:
        while time.monotonic() < deadline:
            data = mic.read()
            if not data:
                continue
            total += len(data)
            samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(samples * samples) + 1e-12))
            peak = max(peak, rms)
            print(f"  rms {rms:6.4f} |{'#' * int(min(rms, 0.5) * 100):<50}|")
    finally:
        mic.stop()

    print(f"\n{total} bytes, peak RMS {peak:.4f}.")
    if total == 0:
        print("Nothing captured at all -- the device opened but delivered no audio.")
        return 1
    if peak < 0.005:
        print("Effectively silent. Check the mic is not muted and that the capture")
        print("level is up:  alsamixer -c <card>  (F4 for capture, M to unmute).")
    return 0


def _say(config: Config, text: str) -> int:
    try:
        from intelligence.config import Config as IntelligenceConfig
        from intelligence.tts import synthesize_pcm
    except ImportError as exc:
        print(f"cannot synthesise: {exc}")
        return 1

    speaker = SpeakerPlayback(
        sample_rate=config.speaker.sample_rate, device=config.speaker.device or None
    )
    ok, why = speaker.available()
    if not ok:
        print(f"cannot play: {why}")
        return 1

    print(f"Synthesising {text!r}...")
    pcm = synthesize_pcm(text, IntelligenceConfig.load(), target_rate=config.speaker.sample_rate)
    print(f"Playing {pcm.duration_s:.1f}s on {speaker.device_name()}")
    speaker.play(pcm.data)
    # Wait for it to be *audible*, not merely written: the pipe accepts the
    # whole reply long before the speaker has finished with it, and exiting
    # here would kill `aplay` mid-sentence.
    while speaker.is_playing():
        time.sleep(0.1)
    speaker.stop()
    return 0


def _transcribe(config: Config, seconds: float) -> int:
    try:
        from intelligence.asr import Session, availability
        from intelligence.config import Config as IntelligenceConfig
    except ImportError as exc:
        print(f"cannot transcribe: {exc}")
        return 1

    cfg = IntelligenceConfig.load()
    avail = availability(cfg)
    if not avail.ok:
        print(f"cannot transcribe: {avail.reason}")
        return 1

    mic = MicCapture(
        sample_rate=config.mic.sample_rate,
        device=config.mic.device or None,
        chunk_ms=config.mic.chunk_ms,
    )
    ok, why = mic.available()
    if not ok:
        print(f"cannot transcribe: {why}")
        return 1

    from ..endpointer import Endpoint, Endpointer

    session = Session(cfg, sample_rate=config.mic.sample_rate)
    endpointer = Endpointer(config.endpointer, sample_rate=config.mic.sample_rate)
    print(f"Say something (up to {seconds:g}s)...")
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            data = mic.read()
            if not data:
                continue
            result = session.accept(data)
            if result is not None and result.text.strip():
                print(f"\nheard: {result.text!r}")
                return 0
            if endpointer.accept(data) in (Endpoint.ENDED, Endpoint.TOO_LONG):
                break
            partial = session.partial()
            if partial:
                print(f"  ...{partial}", end="\r", flush=True)
    finally:
        mic.stop()

    final = session.final()
    print(f"\nheard: {final.text!r}" if final.text.strip() else "\nheard nothing")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="neo-audio-check",
        description="Check the microphone, speaker, wake word and speech path.",
    )
    parser.add_argument("--config", help="one config file; nothing is layered over it")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--listen", action="store_true", help="live wake word scores")
    action.add_argument("--record", action="store_true", help="record and report levels")
    action.add_argument("--say", metavar="TEXT", help="synthesise TEXT and play it")
    action.add_argument("--transcribe", action="store_true", help="transcribe what you say")
    parser.add_argument("--seconds", type=float, default=10.0, help="how long to run (default 10)")
    args = parser.parse_args(argv)

    config = Config.load(args.config)

    if args.listen:
        return _listen(config, args.seconds)
    if args.record:
        return _record(config, args.seconds)
    if args.say:
        return _say(config, args.say)
    if args.transcribe:
        return _transcribe(config, args.seconds)

    source = config.source_path or "defaults"
    print(f"Neo audio check    (config: {source})\n")
    have_mic = _report_devices()
    print()
    have_model = _report_wakeword(config)
    print()
    _report_speech(config)
    print()
    if have_mic and have_model:
        print("Next: neo-audio-check --listen   (say the wake word; watch the scores)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

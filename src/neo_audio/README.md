# neo_audio

The robot's ears and voice: the USB microphone, the speaker, the wake word
"NEO", speech to text for the window the wake word opens, and Piper speaking
the reply.

| Module | What it is |
|---|---|
| `wakeword.py` | A Teachable Machine audio export run in numpy: the browser's spectrogram front end and the network, no TensorFlow |
| `endpointer.py` | When the speaker has finished, from energy against an adaptive noise floor |
| `devices.py` | `arecord` / `aplay` as restartable streams, so an unplugged USB device is recoverable |
| `config.py` | `config/audio.yaml` with `config/audio.local.yaml` merged over it |
| `nodes/mic_hw.py` | USB mic → `/audio/in`, while the mic source is hardware |
| `nodes/speaker_hw.py` | `/audio/out` → USB speaker, and `/audio/playing` for the half-duplex gate |
| `nodes/wake_word.py` | `/audio/in` → `/wake/event`, muted while Neo is speaking or mid-turn |
| `nodes/asr_router.py` | Vosk over the window `/wake/event` opens → `/dialog/transcript` |
| `nodes/tts.py` | `/dialog/reply` → `/audio/out`, a sentence at a time |
| `scripts/audio_check.py` | `neo-audio-check` |

Everything except `nodes/` imports no ROS, and is what the tests drive.

## The wake word

1. In [Teachable Machine](https://teachablemachine.withgoogle.com/), make an
   **Audio project**: the *Background Noise* class, and one class of you saying
   "Neo". Its name does not matter; the wake class is found by position.
2. *Export model → TensorFlow.js → Download*. Put the `.zip` in
   `models/wakeword/` — it does not need unpacking.
3. Point `config/audio.local.yaml` at it (`setup-user.sh` does this for you if
   the zip is already there):

   ```yaml
   wakeword:
     model_path: models/wakeword/neo_wakeword_teachable_machine.zip
   ```

4. Check and tune it on the robot:

   ```bash
   neo-audio-check            # devices found, model loads, speech available
   neo-audio-check --listen   # live scores while you say "Neo"
   ```

   Set `wakeword.threshold` just under the score your "Neo" reaches, and well
   above what ordinary speech reaches. The admin panel's Audio tab shows the same
   live score while the robot is running.

**Record speech into Background Noise, not only silence.** A background class
recorded in a quiet room teaches the model *speech versus silence*, and then
every word fires it. Measured on this robot's first export, fed Piper's voice:
"neo" scored 1.00 — and so did "hello" (0.998), while "banana" scored 0.49.
Piper is not the voice the model was trained on, so the real figures will
differ, but the pattern is the tell. Add ordinary conversation, other words and
the foyer's own noise to Background Noise, and the second class becomes *Neo
versus everything else*.

### Why it runs without TensorFlow

A Teachable Machine audio export is the whole speech-commands network — four
convolutions, a 2000-unit dense layer and the trained two-class head, 1.43 M
parameters — not a head that needs a separately hosted base. Four operations
cover all of it, so it runs as array maths in about 5 ms a window.

The part that has to be exact is the **spectrogram**, because the model was
trained on what a browser's Web Audio `AnalyserNode` produced: audio at
44.1 kHz, a 2048-point FFT every 1024 samples under Web Audio's Blackman window,
magnitudes in dB, the first 232 bins (0–5 kHz), 43 frames, then normalised to
zero mean and unit variance. `BrowserFft` reproduces that. The normalisation is
also why the one awkward detail — how Web Audio scales magnitudes — cannot
matter: a constant scale is a constant dB offset, and the mean removes it.

The mic captures at 16 kHz, which is what Vosk wants. The wake word resamples to
44.1 kHz internally; the model only reads 0–5 kHz, all of which 16 kHz keeps.

### Detection rules

| Setting | Default | Why |
|---|---|---|
| `threshold` | 0.85 | a false accept opens the mic and sends the room to the model host |
| `threshold_with_person` | 0.70 | someone at the desk is far likelier to be addressing Neo; `WakeEvent.threshold_applied` records which applied |
| `consecutive_hits` | 2 | windows overlap by ~85%, so a real word clears several and a door slam clears one |
| `refractory_s` | 2.0 | one "Neo" must not fire on every window that still contains it |
| `hop_frames` | 6 | one evaluation every 139 ms, leaving the cores to YOLO |

The detector is muted while `/audio/playing` is true plus 1.2 s — its window is a
second long, so Neo's last word is still in it when the speaker stops — and
while `/dialog/state` is anything but IDLE.

## The turn

```
/audio/in ──> wake_word ──/wake/event──> asr_router ──/dialog/transcript──> dialog
                                  (pre-roll 0.4 s, endpointer)                 │
/audio/out <── tts <──────────────────────────/dialog/reply────────────────────┘
```

`asr_router` keeps the last 0.4 s of audio before the wake event, because "Neo,
where is CS-204" is one breath, and strips a leading "neo" from what that
captures. It always sends exactly one final transcript per window — an empty one
for a window where nobody spoke — so the dialog node never waits on a turn that
is not coming.

## Running it

```bash
bash scripts/pi/neo-up.sh             # the whole robot, 'hardware' profile
neo-audio-check --record              # levels, to tell a muted mic from a bad model
neo-audio-check --say "hello"         # Piper through the speaker
neo-audio-check --transcribe          # say something; see what Vosk heard
```

On a Raspberry Pi the 3.5 mm jack is off (it shares the servos' PWM hardware),
so the speaker must be USB; `devices.py` prefers any output that is not HDMI.

# neo_sources

Which backend — the Pi's own devices or a browser's, through the admin panel —
owns the camera, the microphone and the speaker. One node, `source_manager`.

| Topic / service | |
|---|---|
| `/sources/set` (`neo_msgs/SetSource`) | switch one stream; refuses an unknown stream or backend rather than clamping it |
| `/sources/state` (`neo_msgs/SourceState`) | latched, so a backend that starts late still learns the selection |
| `/camera/webapp/image_raw` → `/camera/image_raw` | forwarded only while camera = webapp |
| `/audio/webapp/in` → `/audio/in` | forwarded only while mic = webapp, renumbered |
| `/audio/out` → `/audio/webapp/out` | forwarded only while speaker = webapp |
| `/audio/playing` | estimated from the browser's play cursor while speaker = webapp |

The hardware backends (`camera_hw`, `mic_hw`, `speaker_hw`) are not started or
stopped by this node. They subscribe to `/sources/state` and gate themselves,
releasing the device when their stream moves to the browser. A crash here
therefore cannot leave a microphone hot, and a browser tab left open cannot
inject frames or audio into a robot that is using its own devices.

The boot selection comes from the launch profile's `sources:` block, passed as
the `initial_camera`, `initial_mic` and `initial_speaker` parameters.

`selection.py` and `playback.py` import no ROS and are what the tests drive.

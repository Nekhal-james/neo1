# One block of PCM audio, on /audio/in (mic) or /audio/out (TTS).
#
# Carries its own format rather than relying on a parameter agreed out of band,
# because the two directions genuinely differ -- 16 kHz in, 22.05 kHz out -- and
# because the mic backend can change at runtime between the Pi's USB mic and a
# browser's, which do not necessarily agree on rate.

std_msgs/Header header

# Monotonic per-stream counter. A gap here is a dropped chunk, which is the
# difference between "the speaker cut out" and "the pipeline stalled"; without
# it a consumer cannot tell those apart from timestamps alone.
uint32 seq

uint32 sample_rate       # 16000 on /audio/in, 22050 on /audio/out
uint8 channels           # 1 -- mono throughout; stereo is not in scope

uint8 ENCODING_PCM_S16LE = 0
uint8 encoding

# Raw interleaved samples in `encoding`. Length is a whole number of frames:
# len(data) == frames * channels * bytes_per_sample.
uint8[] data

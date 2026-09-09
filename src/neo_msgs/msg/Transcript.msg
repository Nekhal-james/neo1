# What the listener heard, on /dialog/transcript.

std_msgs/Header header

string text
bool is_final
float32 confidence

# Which engine produced this. Published rather than inferred, because the
# selection is a runtime policy (Vosk always available on the Pi; Whisper only
# while the link is up, plan 5.4) and a bad transcript is not diagnosable
# without knowing which one produced it.
uint8 ENGINE_VOSK = 0      # on-Pi, always available
uint8 ENGINE_WHISPER = 1   # off-board, accuracy upgrade only
uint8 engine

# Vosk was run with a grammar built from the room list, which sharply improves
# codes like "CS-204" but cannot return anything outside it. Worth knowing when
# a transcript comes back suspiciously close to a real room.
bool grammar_constrained

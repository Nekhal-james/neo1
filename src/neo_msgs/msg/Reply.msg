# What Neo is about to say, on /dialog/reply.

std_msgs/Header header

string text

# Where the words came from.
#
# SOURCE_KB is the primary path, not a fallback: the info-desk role works end
# to end with no LLM at all (CLAUDE.md), and with the laptop usually away it is
# what most people will actually hear.
uint8 SOURCE_KB = 0         # template renderer over the campus dataset, local
uint8 SOURCE_LLM = 1        # off-board Qwen
uint8 SOURCE_KB_POLISHED = 2  # KB facts, LLM phrasing -- facts unchanged
uint8 SOURCE_FALLBACK = 3   # an honest admission; neither path could answer
uint8 source

float32 latency_ms

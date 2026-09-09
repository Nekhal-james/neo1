# The wake phrase "NEO" fired. Published on /wake/event.
#
# Neo is silent and non-responsive until this fires (CLAUDE.md). The detector is
# upstream of ASR, not a filter applied after it, so this message is the only
# thing that opens a listening window.

std_msgs/Header header

float32 score

# The threshold this event was judged against -- not the configured one.
#
# They differ: the threshold relaxes while a person is in frame (plan 5.2
# cross-modal gating). Recording the applied value is what makes a false accept
# diagnosable after the fact; the configured value alone would not explain it.
float32 threshold_applied

# Whether /perception/attention reported a person when this fired, i.e. whether
# the relaxed threshold was in force.
bool person_present

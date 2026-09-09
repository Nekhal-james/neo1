# How much of the campus Neo has actually learned, on /kb/coverage.
#
# Latched-style: published on load and on every hot reload after a panel edit
# (plan 7.3), not periodically. Campus data arrives over months, so this is the
# message that keeps Neo's honesty in step with a dataset that grew five minutes
# ago without a restart.

std_msgs/Header header

neo_msgs/BlockCoverage[] blocks

uint32 total_rooms

# Bumped on every reload. A consumer that caches an answer can tell its cache
# is stale without diffing the whole coverage set.
uint32 revision

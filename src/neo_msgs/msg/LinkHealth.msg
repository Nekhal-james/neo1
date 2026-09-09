# Reachability of the off-board host, on /link/health.
#
# Published only by llm_client / link_node -- the one node permitted to talk to
# the laptop (plan 6). Mirrored in Python by neo_webapp.bridge.types.LinkHealth,
# which the panel reads from model_conn's status file when ROS is not running.

std_msgs/Header header

bool up

# Which of the ordered endpoints answered. Ethernet is preferred and tried
# first; Wi-Fi is the failover, and may not exist at all if campus AP isolation
# blocks client-to-client traffic (plan 0.3.2).
uint8 PATH_NONE = 0
uint8 PATH_ETH = 1
uint8 PATH_WIFI = 2
uint8 active_path

# Round trip of the last successful probe. Meaningless while `up` is false;
# readers must gate on `up` rather than on a sentinel value here.
float32 rtt_ms

# Consecutive failed probes. Zero whenever `up` is true, so this counts the
# current outage, not the lifetime total -- which is what tells "flapping" from
# "the laptop has been shut for an hour".
uint32 consecutive_failures

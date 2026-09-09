# Where the head should look, and whether anyone has actually asked for it.
#
# Published on /perception/attention by the perception node. Mirrored in pure
# Python by neo_perception.types.AttentionTarget, which is what the admin panel
# consumes today -- the two must not drift (see neo_msgs/test/test_contract.py).

std_msgs/Header header

# Normalised camera coordinates in [-1, 1], y positive UPWARD.
#
# Upward, not image-down: this feeds the head the same way the panel's virtual
# joystick does, and one convention across both is what stops a sign error
# turning "look up" into "look down" somewhere in the arbiter.
float32 x
float32 y

float32 confidence

# Track id of the attended person, or NO_TRACK when nothing is attended.
int32 NO_TRACK = -1
int32 track_id

# Someone is in frame. Consumed by the wake word for cross-modal gating
# (plan 5.2) and by the emotion node -- both of which care about presence
# even when nobody has engaged.
bool person_present

# Someone has engaged: presence alone never moves the head, a held gesture
# does (plan 4.0). `engaged` false with `person_present` true is the normal
# state of a corridor desk with people walking past.
bool engaged

uint8 SCANNING = 0    # nobody engaged; watching for a gesture
uint8 ENGAGING = 1    # gesture seen, confirming it is deliberate
uint8 ENGAGED = 2     # locked and tracking
uint8 SUSPENDED = 3   # locked target not visible; a grace timer is running
uint8 state

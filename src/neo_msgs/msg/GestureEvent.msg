# A static pose recognised on a tracked person, on /perception/gestures.
#
# Static only. At the ~4 fps a Pi 4 can spare, a 2 Hz wave aliases into noise,
# so WAVE is defined here for completeness but is not what engagement runs on
# (plan 4.0).

std_msgs/Header header

int32 track_id

uint8 NONE = 0

# Hand above the shoulder at any arm angle. Loose: it also fires on stretching,
# scratching your head, or reaching for a shelf.
uint8 RAISED_HAND = 1

# Hand up with a roughly vertical forearm -- a palm presented to the camera.
# The default engage gesture, because the verticality requirement is what
# separates "I want your attention" from "I am scratching my head".
#
# COCO-17 has no finger keypoints, so this is a pose, not a hand shape: a
# raised fist is indistinguishable. Telling them apart needs a hand-landmark
# model on a wrist crop, which is a per-frame cost the Pi has not got.
uint8 OPEN_PALM = 2

uint8 WAVE = 3
uint8 kind

float32 confidence

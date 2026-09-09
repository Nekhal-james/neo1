# The emotional state, on /emotion/state.
#
# A movement *modifier*, never a second actuator path (CLAUDE.md). This message
# carries no pan/tilt: head_behavior blends these parameters into the command it
# was going to send anyway. emotion_node has no route to servo_driver.

std_msgs/Header header

uint8 NEUTRAL = 0
uint8 ATTENTIVE = 1
uint8 HAPPY = 2
uint8 CURIOUS = 3
uint8 CONFUSED = 4
uint8 SLEEPY = 5
uint8 label

float32 intensity   # [0, 1]

# Motion parameters (plan 9.1). Shipped with the label rather than looked up
# from a table by each consumer, so the Emotion tab's live sliders and the
# running behaviour cannot disagree about what CURIOUS currently means.
float32 idle_amplitude_deg   # size of idle drift
float32 idle_freq_hz         # speed of idle motion
float32 gaze_gain            # how eagerly it snaps to a person
float32 gaze_lag             # how smoothly, in seconds
float32 tilt_bias_deg        # resting head tilt; curiosity reads as a tilt
float32 micro_motion         # small high-frequency movement; its absence reads as "asleep"
float32 settle_time          # how long it holds still after arriving, in seconds

# These are inputs to a blend, not commands: every Phase 3 soft limit and rate
# cap still applies downstream, for any values at all. servo_driver is the
# authority on what the servos are allowed to do.

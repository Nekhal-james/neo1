# The conversation state machine, on /dialog/state.
#
# DELIBERATE DEVIATION from the plan's section 1.2, which lists a single flat
# enum of IDLE, LISTENING, THINKING, SPEAKING, DEGRADED, ESTOP. Section 8.1 of
# the same document calls DEGRADED "a modifier" and ESTOP "an override", and
# that reading is the correct one:
#
#   The off-board laptop is usually away, so degraded is the *normal* operating
#   state (plan 0.3.3), and Neo runs the full IDLE -> LISTENING -> THINKING ->
#   SPEAKING cycle inside it -- answering room questions entirely locally. A
#   flat enum cannot express "listening, with the link down", so it would force
#   every consumer to choose between showing the machine state and showing the
#   link state, and the panel needs both at once.
#
# So: four machine states, two orthogonal flags. The panel still renders one
# badge string, flattening these back to the plan's six labels for display --
# see neo_webapp.bridge.types.DIALOG_STATES.

std_msgs/Header header

uint8 IDLE = 0        # gated: the wake word owns the mic, ASR is not subscribed
uint8 LISTENING = 1   # wake fired; capturing until the endpointer closes
uint8 THINKING = 2    # retrieving and/or generating
uint8 SPEAKING = 3    # TTS is playing; the mic is muted (half-duplex, plan 5.6)
uint8 state

# The off-board host is unreachable. Info-desk answers are unaffected -- they
# never needed it -- so this is not an error, and no state above is unreachable
# while it is set.
bool degraded

# /system/estop is asserted. Overrides everything: motion is frozen and no
# transition above may proceed until it clears.
bool estop

# Set during the follow-up window after speaking (plan 8.3), when a further
# question is accepted without repeating the wake word. Distinct from
# LISTENING: no capture is running yet, the gate is merely open.
bool follow_up_open

# A request to point the head, on /head/command at 50 Hz.
#
# A request, not an order: servo_driver clamps to its own soft limits and
# slew-rate caps before driving anything, and never trusts an upstream node
# (plan 3.2). A command outside the limits is silently clamped, not rejected.

std_msgs/Header header

# Radians, not degrees. The plan's section 1.2 fixes rad here, and /head/state
# is a sensor_msgs/JointState, which is rad by ROS convention -- so the whole
# motion path is one unit and no node converts. The admin panel shows degrees
# and converts at its own edge; that boundary is the only place a factor of
# 180/pi belongs.
#
# Zero is centre. Pan is positive to the robot's left, tilt positive up.
float32 pan_rad
float32 tilt_rad

# Ceiling on how fast to get there, <= 0 meaning "use the driver's default".
# The driver's own cap still applies and is always the lower of the two.
float32 max_speed_rad_s

# Arbitration in head_behavior (plan 3.3): highest number wins, and the winner
# is crossfaded in over ~300 ms rather than jumped to.
#
# Gaps are left between levels so a source can be inserted later -- a
# navigation source, when there is a body to navigate (plan 11) -- without
# renumbering the ones already deployed.
uint8 PRIORITY_IDLE = 10      # idle drift and micro-motion
uint8 PRIORITY_GAZE = 30      # tracking an engaged person
uint8 PRIORITY_GESTURE = 50   # a bounded nod/shake/tilt overlay
uint8 PRIORITY_MANUAL = 70    # the panel's virtual joystick
uint8 PRIORITY_ESTOP = 90     # freeze
uint8 priority

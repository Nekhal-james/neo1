# Which backend is live for each stream, on /sources/state.
#
# Published by source_manager after every transition. The constants here are the
# whole vocabulary of the hardware/webapp seam -- no node downstream of a mux
# ever sees them, by design (plan 1.2: "no node subscribes to a <ns>/hw/<topic>
# or <ns>/webapp/<topic> topic except its own mux").
#
# Written with <ns> rather than a glob on purpose: rosidl copies these comments
# verbatim into generated C block comments, so a star-slash sequence here would
# close that comment early and the generated header would not compile.

std_msgs/Header header

uint8 BACKEND_HARDWARE = 0   # Pi Camera (CSI), USB mic, attached speaker
uint8 BACKEND_WEBAPP = 1     # the browser device's camera/mic/speaker

uint8 camera
uint8 mic
uint8 speaker

# Set while a switch is in flight, so the panel can disable the control instead
# of letting an operator queue up conflicting switches. A transition is
# activate-new, wait for ACTIVE, switch the mux, deactivate-old -- and it rolls
# back on failure rather than leaving both backends inactive (plan 2.1).
uint8 STREAM_NONE = 0
uint8 STREAM_CAMERA = 1
uint8 STREAM_MIC = 2
uint8 STREAM_SPEAKER = 3
uint8 transitioning

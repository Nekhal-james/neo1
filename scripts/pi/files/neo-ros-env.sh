# Neo: ROS 2 settings every shell on the robot must agree on (plan Phase 0, step 5).
# Installed to /etc/profile.d/ by scripts/pi/setup-system.sh.
#
# Deliberately does NOT source /opt/ros/jazzy/setup.bash. The repo's pytest
# suites run from a pip venv, and ROS's launch_testing plugin breaks every pytest
# run in a shell where both are active -- see CLAUDE.md. Source ROS explicitly
# in the shells that need it.
export ROS_DOMAIN_ID=42
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI=file:///etc/neo/cyclonedds.xml

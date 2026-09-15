#!/usr/bin/env bash
# Start the admin panel with the robot's ROS graph visible to it.
#
#     bash scripts/pi/neo-panel.sh            # what neo-panel.service runs
#
# `neo --webapp up` on its own works, but on the robot it does the wrong thing
# silently: with ROS not sourced it cannot import rclpy, so its bridge falls
# back to the simulated robot. The panel then loads, logs in, moves a simulated
# head and shows a simulated conversation, while the real robot beside it does
# nothing -- and nothing says so except "backend: mock" in a corner.
#
# Sourcing ROS for the panel is safe. The venv-plus-ROS conflict in CLAUDE.md is
# pytest's alone (launch_testing's plugin); this process never runs pytest.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV="${NEO_VENV:-$HOME/neo-venv}"
ROS_SETUP="${NEO_ROS_SETUP:-/opt/ros/jazzy/setup.bash}"

[ -x "$VENV/bin/neo" ] || {
  echo "error: no 'neo' command in $VENV -- run scripts/pi/setup-user.sh" >&2
  exit 1
}

unset CONDA_PREFIX CONDA_DEFAULT_ENV CONDA_SHLVL CONDA_PROMPT_MODIFIER VIRTUAL_ENV

# ROS's setup.bash reads variables it has not set, so -u comes off around it.
set +u
# Same domain and RMW as the graph, or the two never discover each other.
# shellcheck disable=SC1091
[ -f /etc/profile.d/neo-ros-env.sh ] && source /etc/profile.d/neo-ros-env.sh
if [ -f "$ROS_SETUP" ] && [ -f "$REPO_DIR/install/setup.bash" ]; then
  # shellcheck disable=SC1090
  source "$ROS_SETUP"
  # shellcheck disable=SC1091
  source "$REPO_DIR/install/setup.bash"
else
  echo "note: no ROS workspace here -- the panel will run its simulated robot" >&2
fi
set -u

cd "$REPO_DIR"
exec "$VENV/bin/neo" --webapp up "$@"

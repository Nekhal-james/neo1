#!/usr/bin/env bash
# Bring Neo up with one command.
#
#     bash scripts/pi/neo-up.sh                 # the 'hardware' profile
#     bash scripts/pi/neo-up.sh --profile dev   # no hardware needed
#     bash scripts/pi/neo-up.sh --panel         # also start the admin panel
#     bash scripts/pi/neo-up.sh --check         # say what would run, start nothing
#
# This exists because starting the robot by hand needs three things right at
# once, and getting any of them wrong fails in a way that does not name itself:
#
#   1. ROS sourced, and the workspace overlay after it.
#   2. The venv's packages reachable *without* activating the venv. ROS runs its
#      nodes under the system interpreter, and a venv activated in the same
#      shell breaks colcon (CLAUDE.md). But vosk, piper and the repo itself live
#      in the venv -- so the wake word and the voice die at import with a
#      "not installed" that is untrue, because they are installed, just not
#      where that interpreter was looking.
#   3. No conda. Its markers make ROS's own Python discovery prefer it.
#
# The fix for (2) is to APPEND the venv's site-packages to PYTHONPATH rather
# than prepend. ROS's numpy and yaml keep priority -- they are the versions its
# own C extensions were built against -- and the venv only fills in what the
# system interpreter genuinely does not have.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV="${NEO_VENV:-$HOME/neo-venv}"
ROS_SETUP="${NEO_ROS_SETUP:-/opt/ros/jazzy/setup.bash}"
PROFILE="${NEO_PROFILE:-hardware}"
WITH_PANEL=0
CHECK_ONLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --profile) PROFILE="${2:?--profile needs a name}"; shift 2 ;;
    --panel)   WITH_PANEL=1; shift ;;
    --check)   CHECK_ONLY=1; shift ;;
    -h|--help) sed -n '2,28p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

say() { printf '==> %s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ -f "$ROS_SETUP" ] || fail "no ROS at $ROS_SETUP -- run scripts/pi/setup-system.sh first"
[ -f "$REPO_DIR/install/setup.bash" ] || fail \
  "no colcon workspace at $REPO_DIR/install -- run scripts/pi/setup-user.sh first"

# Conda and an active venv both confuse ROS's interpreter discovery; drop every
# marker they leave behind rather than only fixing PATH.
unset CONDA_PREFIX CONDA_DEFAULT_ENV CONDA_SHLVL CONDA_PROMPT_MODIFIER VIRTUAL_ENV
export PATH="/usr/bin:/bin:$PATH"

# ROS's setup.bash reads variables it has not set yet (AMENT_TRACE_SETUP_FILES
# among them), so `set -u` has to come off across the sourcing or bringup dies
# on ROS's own boilerplate rather than on anything to do with this robot.
set +u
# The DDS settings every process on the robot must share: domain, RMW, the
# interface pinning. systemd does not read /etc/profile.d, so without this a
# service-started graph would sit on domain 0, invisible to a panel on 42.
# shellcheck disable=SC1091
[ -f /etc/profile.d/neo-ros-env.sh ] && source /etc/profile.d/neo-ros-env.sh
# shellcheck disable=SC1090
source "$ROS_SETUP"
# shellcheck disable=SC1091
source "$REPO_DIR/install/setup.bash"
set -u

if [ -x "$VENV/bin/python" ]; then
  # Ask the venv where its packages are rather than assuming python3.12: the
  # path changes with the interpreter, and a wrong guess here is silent.
  SITE="$("$VENV/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
  export PYTHONPATH="${PYTHONPATH:-}:$SITE"
  say "venv packages: $SITE (appended, so ROS keeps its own numpy)"
else
  say "no venv at $VENV -- the wake word and voice will be unavailable"
fi

export NEO_REPO_DIR="$REPO_DIR"
cd "$REPO_DIR"

if [ "$CHECK_ONLY" = 1 ]; then
  say "profile:  $PROFILE"
  say "python:   $(command -v python3)"
  for module in rclpy neo_msgs numpy vosk piper; do
    if python3 -c "import $module" 2>/dev/null; then
      printf '    %-10s ok\n' "$module"
    else
      printf '    %-10s MISSING\n' "$module"
    fi
  done
  say "would run: ros2 launch neo_bringup neo.launch.py profile:=$PROFILE"
  [ "$WITH_PANEL" = 1 ] && say "would run: scripts/pi/neo-panel.sh (in the background)"
  exit 0
fi

if [ "$WITH_PANEL" = 1 ]; then
  # The panel is a separate process on purpose: it is started in any order,
  # often from a different machine, and shares state through var/ (CLAUDE.md).
  # Backgrounding it here is a convenience, not a coupling -- it keeps running
  # if the graph stops, which is exactly when you want to look at it.
  #
  # Through neo-panel.sh, which keeps ROS sourced. Without ROS in its
  # environment the panel cannot import rclpy, and it falls back to its
  # simulated robot *quietly* -- a panel that looks fine and controls nothing.
  [ -x "$VENV/bin/neo" ] || fail "no 'neo' command in $VENV -- run scripts/pi/setup-user.sh"
  say "admin panel: starting in the background (logs: var/panel.log)"
  mkdir -p "$REPO_DIR/var"
  bash "$REPO_DIR/scripts/pi/neo-panel.sh" >>"$REPO_DIR/var/panel.log" 2>&1 &
  echo $! > "$REPO_DIR/var/panel.pid"
fi

say "profile '$PROFILE' -- ctrl-C to stop"
exec ros2 launch neo_bringup neo.launch.py "profile:=$PROFILE"

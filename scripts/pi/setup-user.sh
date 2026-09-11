#!/usr/bin/env bash
# Neo: the per-user half of the Pi setup. Run as the robot's user, NOT root,
# after scripts/pi/setup-system.sh:
#
#     bash ~/neo1/scripts/pi/setup-user.sh [--skip-tests]
#
# Creates ~/neo-venv, installs the repo with the extras the robot uses, writes
# the per-machine config files that are missing (never overwriting one), builds
# the two ROS packages, and runs every test suite. Idempotent.
#
# Environment overrides: NEO_VENV (default ~/neo-venv),
# NEO_EXTRAS (default dev,detector,voice), NEO_LAPTOP_ETH (default 192.168.50.1).

set -euo pipefail

ROS_DISTRO=jazzy
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV="${NEO_VENV:-$HOME/neo-venv}"
EXTRAS="${NEO_EXTRAS:-dev,detector,voice}"
LAPTOP_ETH="${NEO_LAPTOP_ETH:-192.168.50.1}"
RUN_TESTS=1
[ "${1:-}" = "--skip-tests" ] && RUN_TESTS=0

step() { printf '\n==> %s\n' "$*"; }

if [ "$(id -u)" -eq 0 ]; then
  echo "do not run this as root; it builds your user's venv and ROS workspace" >&2
  exit 1
fi
if [ ! -f "/opt/ros/$ROS_DISTRO/setup.bash" ]; then
  echo "ROS 2 $ROS_DISTRO is not installed; run: sudo bash $REPO_DIR/scripts/pi/setup-system.sh" >&2
  exit 1
fi

# In a subshell with ROS sourced and no venv -- the one combination CLAUDE.md
# allows for colcon. ROS's setup scripts read unset variables, hence +u.
ros_shell() {
  (
    set +u
    # shellcheck disable=SC1090
    source "/opt/ros/$ROS_DISTRO/setup.bash"
    set -u
    cd "$REPO_DIR"
    "$@"
  )
}

step "Python venv at $VENV"
# --system-site-packages keeps rclpy importable once ROS is sourced, which the
# panel's ROS bridge needs -- the same shape as the WSL dev setup.
[ -x "$VENV/bin/python" ] || python3 -m venv --system-site-packages "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip wheel

step "repo with extras [$EXTRAS]"
# torch arrives with ultralytics. PyPI's aarch64 wheels are CPU-only already, so
# the multi-gigabyte CUDA download that x86 Linux has to dodge does not happen.
"$VENV/bin/python" -m pip install -e "$REPO_DIR[$EXTRAS]"

step "per-machine config (only files that do not exist yet)"
mkdir -p "$REPO_DIR/config"
if [ ! -f "$REPO_DIR/config/intelligence.local.yaml" ]; then
  cat > "$REPO_DIR/config/intelligence.local.yaml" <<YAML
# Written by scripts/pi/setup-user.sh. Relative paths resolve against the repo
# root; sync-to-pi.sh copies models/ from the laptop.
asr:
  vosk_model_path: models/vosk-model-small-en-in-0.4
tts:
  piper_model_path: models/en_US-lessac-medium.onnx
  piper_config_path: models/en_US-lessac-medium.onnx.json
YAML
  echo "  wrote config/intelligence.local.yaml"
else
  echo "  config/intelligence.local.yaml exists; left alone"
fi
if [ ! -f "$REPO_DIR/config/model_conn.local.yaml" ]; then
  cat > "$REPO_DIR/config/model_conn.local.yaml" <<YAML
# Written by scripts/pi/setup-user.sh. The laptop's end of the direct cable,
# per plan Phase 0 (Pi 192.168.50.2, laptop 192.168.50.1).
receiver:
  endpoints:
    - name: eth
      host: $LAPTOP_ETH
      port: 11434
    - name: wifi
      host: neo-brain.local
      port: 11434
YAML
  echo "  wrote config/model_conn.local.yaml (eth -> $LAPTOP_ETH)"
else
  echo "  config/model_conn.local.yaml exists; left alone"
fi

missing_models=0
for path in models/vosk-model-small-en-in-0.4 models/en_US-lessac-medium.onnx yolov8n-pose.pt; do
  [ -e "$REPO_DIR/$path" ] || { echo "  missing: $path (run sync-to-pi.sh from the laptop)"; missing_models=1; }
done
[ "$missing_models" = 0 ] && echo "  speech and pose models present"

step "colcon build: neo_msgs + neo_bringup (ROS shell, no venv)"
ros_shell colcon build --base-paths src --symlink-install
if [ ! -d "$REPO_DIR/install/neo_msgs" ]; then
  echo "colcon built nothing -- see CLAUDE.md on --base-paths src" >&2
  exit 1
fi

if [ "$RUN_TESTS" = 1 ]; then
  failures=0

  step "pytest, per package (venv only, ROS NOT sourced -- see CLAUDE.md)"
  for pkg in neo_webapp neo_perception neo_motion neo_emotion intelligence model-conn neo_msgs neo_bringup; do
    log="/tmp/neo-pytest-$pkg.log"
    printf '  %-16s ' "$pkg"
    if (cd "$REPO_DIR/src/$pkg" && env -u AMENT_PREFIX_PATH -u PYTHONPATH -u ROS_DISTRO \
          "$VENV/bin/python" -m pytest -q -p no:cacheprovider >"$log" 2>&1); then
      tail -1 "$log"
    else
      echo "FAILED -- $log"
      failures=1
    fi
  done

  step "colcon test (ROS shell, system pytest)"
  if ros_shell colcon test --base-paths src --event-handlers console_direct- >/tmp/neo-colcon-test.log 2>&1 \
      && ros_shell colcon test-result --verbose; then
    :
  else
    echo "colcon test failed -- /tmp/neo-colcon-test.log"
    failures=1
  fi

  if [ "$failures" != 0 ]; then
    echo "some suites failed; see the logs above" >&2
    exit 1
  fi
fi

step "user setup complete"
cat <<NEXT
Still yours to do (each needs a password or a secret, so no script does it):

  1. Admin panel password:     $VENV/bin/neo --webapp setup
  2. Panel certificate:        see docs/pi-setup.md, section 4
  3. Link mTLS:                see docs/security.md

Run the panel:                 $VENV/bin/neo --webapp up
Use ROS in a shell:            source /opt/ros/$ROS_DISTRO/setup.bash && source $REPO_DIR/install/setup.bash
NEXT

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
# NEO_EXTRAS (default dev,detector,voice,servo), NEO_LAPTOP_ETH (default 192.168.50.1).

set -euo pipefail

ROS_DISTRO=jazzy
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV="${NEO_VENV:-$HOME/neo-venv}"
EXTRAS="${NEO_EXTRAS:-dev,detector,voice,servo}"
LAPTOP_ETH="${NEO_LAPTOP_ETH:-192.168.50.1}"
RUN_TESTS=1
[ "${1:-}" = "--skip-tests" ] && RUN_TESTS=0

step() { printf '\n==> %s\n' "$*"; }

if [ "$(id -u)" -eq 0 ]; then
  echo "do not run this as root; it builds your user's venv and ROS workspace" >&2
  exit 1
fi

# The system python3, explicitly -- not whatever "python3" resolves to on
# PATH. A conda/miniforge install with its base environment auto-activated
# (a `(base)` shell prompt) puts its own python3 ahead of /usr/bin's, and ROS's
# apt-installed Python tooling (catkin_pkg, python3-pytest, ament's own deps)
# is installed for /usr/bin/python3, not for whatever environment happened to
# be active in the shell that ran this script. Same failure shape as the pip
# venv + sourced ROS conflict CLAUDE.md already documents -- conda is just
# another python manager that can shadow the interpreter ROS needs.
SYSTEM_PYTHON=/usr/bin/python3
if [ ! -x "$SYSTEM_PYTHON" ]; then
  echo "$SYSTEM_PYTHON not found; run scripts/pi/setup-system.sh first" >&2
  exit 1
fi
if command -v conda >/dev/null 2>&1 || [ -n "${CONDA_PREFIX:-}" ]; then
  echo "note: conda detected in this shell -- forcing $SYSTEM_PYTHON for the venv" \
       "and /usr/bin ahead of PATH inside ros_shell (below), so neither the venv" \
       "nor colcon accidentally build against conda's python3 instead."
fi
if [ ! -f "/opt/ros/$ROS_DISTRO/setup.bash" ]; then
  echo "ROS 2 $ROS_DISTRO is not installed; run: sudo bash $REPO_DIR/scripts/pi/setup-system.sh" >&2
  exit 1
fi

# In a subshell with ROS sourced and no venv -- the one combination CLAUDE.md
# allows for colcon. ROS's setup scripts read unset variables, hence +u.
ros_shell() {
  (
    # Re-prepend the system dirs so `python3` (and anything colcon/ament shell
    # out to) resolves to /usr/bin's on PATH...
    export PATH="/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
    # ...but PATH order alone does not win here: CMake's FindPython3 module
    # explicitly checks CONDA_PREFIX/VIRTUAL_ENV and *prefers* a conda/venv
    # Python over anything found on PATH (Python3_FIND_VIRTUALENV defaults to
    # FIRST) -- so with CONDA_PREFIX still set, cmake picks conda's python3
    # right back regardless of PATH. Measured: reordering PATH alone did not
    # stop `ament_package_xml.cmake` from invoking .../miniforge3/bin/python3.
    # Unsetting the markers both conda and a pip venv leave behind removes the
    # signal CMake keys off, not just one path it might search.
    unset CONDA_PREFIX CONDA_DEFAULT_ENV CONDA_SHLVL CONDA_PROMPT_MODIFIER VIRTUAL_ENV
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
# panel's ROS bridge needs -- the same shape as the WSL dev setup. Built from
# $SYSTEM_PYTHON explicitly, not bare `python3`: a venv built from conda's
# python3 would still take --system-site-packages, just pointed at conda's
# site-packages instead of the system dist-packages rclpy actually lives in --
# wrong silently, not with an error, and only noticed the first time the panel
# tries the `ros` backend.
[ -x "$VENV/bin/python" ] || "$SYSTEM_PYTHON" -m venv --system-site-packages "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip wheel

case ",$EXTRAS," in
  *,detector,*)
    step "PyTorch, CPU-only build (before ultralytics can pull the CUDA one)"
    # ultralytics depends on torch, and PyPI's aarch64 torch is the build for
    # NVIDIA's arm64 servers: measured on the Pi, torch 2.14.0 from PyPI pulled
    # cuDNN, cuBLAS, NCCL and the rest of CUDA 13 -- 1.3 GB cached before it was
    # stopped, several GB to go, for a board with no NVIDIA GPU. PyTorch's own
    # CPU index has the same versions with none of that. Installed first, it
    # satisfies ultralytics' requirement, so pip never looks at PyPI's.
    if "$VENV/bin/python" -c "import torch, sys; sys.exit(0 if '+cpu' in torch.__version__ else 1)" 2>/dev/null; then
      echo "already present: $("$VENV/bin/python" -c 'import torch; print(torch.__version__)')"
    else
      "$VENV/bin/python" -m pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision
    fi
    ;;
esac

step "repo with extras [$EXTRAS]"
"$VENV/bin/python" -m pip install -e "$REPO_DIR[$EXTRAS]"
# nvidia-ml-py is exempt: ultralytics requires it, and it is a 53 kB pure-Python
# binding that does nothing without a GPU. What must not be here is CUDA itself.
cuda_pkgs="$("$VENV/bin/python" -m pip list 2>/dev/null | awk 'tolower($1) ~ /^(nvidia-|cuda-)/ && tolower($1) != "nvidia-ml-py" {print $1}')"
if [ -n "$cuda_pkgs" ]; then
  echo "NVIDIA/CUDA packages were installed into $VENV; the Pi cannot use them:" $cuda_pkgs >&2
  echo "remove them: $VENV/bin/pip uninstall -y $cuda_pkgs" >&2
  exit 1
fi

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

case ",$EXTRAS," in
  *,servo,*)
    step "servo board (read-only: nothing moves)"
    # Informational. No board wired yet is a normal state during setup, so it
    # does not fail the script; the check names what is missing either way.
    "$VENV/bin/neo-servo-check" || echo "(not an error for setup: wire the PCA9685 and re-run $VENV/bin/neo-servo-check)"
    ;;
esac

step "user setup complete"
cat <<NEXT
Still yours to do (each needs a password or a secret, so no script does it):

  1. Admin panel password:     $VENV/bin/neo --webapp setup
  2. Panel certificate:        see docs/pi-setup.md, section 4
  3. Link mTLS:                see docs/security.md

Run the panel:                 $VENV/bin/neo --webapp up
Check the servo board:         $VENV/bin/neo-servo-check   (add --center to move them)
Use ROS in a shell:            source /opt/ros/$ROS_DISTRO/setup.bash && source $REPO_DIR/install/setup.bash
NEXT

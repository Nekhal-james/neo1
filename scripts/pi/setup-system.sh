#!/usr/bin/env bash
# Neo: provision a Raspberry Pi 4 running Ubuntu Server 24.04 LTS (arm64).
#
# Run ON THE PI, from the copy that scripts/pi/sync-to-pi.sh put there:
#
#     sudo bash ~/neo1/scripts/pi/setup-system.sh [options]
#
#   --eth-address CIDR     static address for eth0 (default 192.168.50.2/24)
#   --with-panel-service   install and enable neo-panel.service
#
# The system half of plan Phase 0: packages, ROS 2 Jazzy, device access, mDNS,
# the Ethernet link, DDS pinning. It needs no password and no secret beyond the
# sudo you ran it with. The per-user half -- venv, pip, colcon build, tests -- is
# scripts/pi/setup-user.sh, which must NOT run as root.
#
# Idempotent: re-run it after a failure or after syncing a newer checkout.

set -euo pipefail

ROS_DISTRO=jazzy
ETH_ADDRESS="192.168.50.2/24"
WITH_PANEL_SERVICE=0
REBOOT_NEEDED=0

while [ $# -gt 0 ]; do
  case "$1" in
    --eth-address) ETH_ADDRESS="$2"; shift 2 ;;
    --with-panel-service) WITH_PANEL_SERVICE=1; shift ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
done

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FILES="$REPO_DIR/scripts/pi/files"

step() { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
have_systemd() { [ -d /run/systemd/system ]; }

# --- preconditions -----------------------------------------------------------

if [ "$(id -u)" -ne 0 ]; then
  echo "needs root: sudo bash $0" >&2
  exit 1
fi
TARGET_USER="${SUDO_USER:-}"
if [ -z "$TARGET_USER" ] || [ "$TARGET_USER" = root ]; then
  echo "run it with sudo from the robot's own user (neo), not from a root shell" >&2
  exit 1
fi
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"

# shellcheck disable=SC1091
. /etc/os-release
if [ "${VERSION_CODENAME:-}" != noble ]; then
  echo "this is ${PRETTY_NAME:-an unknown OS}; ROS 2 Jazzy is only packaged for Ubuntu 24.04 (noble)" >&2
  exit 1
fi
ARCH="$(dpkg --print-architecture)"
[ "$ARCH" = arm64 ] || warn "architecture is $ARCH, not arm64: this is not a Raspberry Pi"

step "clock"
# No RTC on a Pi 4. Until network time lands, the clock can sit weeks in the
# past, and apt then rejects every repository as "not valid yet" -- which looks
# like a broken mirror rather than a wrong clock.
if have_systemd && timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -q yes; then
  echo "synchronised: $(date)"
else
  warn "clock not NTP-synchronised yet ($(date)). If apt says a release file is 'not valid yet', wait for 'timedatectl' to show synchronized: yes and re-run."
fi

step "internet"
if ! getent hosts ports.ubuntu.com >/dev/null; then
  echo "no internet from the Pi. Configure Wi-Fi in Raspberry Pi Imager, or share the laptop's connection over the cable -- see docs/pi-setup.md." >&2
  exit 1
fi
echo "ok"

# --- packages ----------------------------------------------------------------

export DEBIAN_FRONTEND=noninteractive

step "base packages"
apt-get update
apt-get install -y --no-install-recommends software-properties-common
add-apt-repository -y universe
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates curl git build-essential \
  python3-venv python3-dev python3-pip \
  i2c-tools v4l-utils alsa-utils iw \
  avahi-daemon libnss-mdns

# Camera tooling for Bench A. Checked rather than assumed: which of these the
# archive carries for a given release has moved around.
for pkg in libcamera-tools rpicam-apps; do
  if apt-cache show "$pkg" >/dev/null 2>&1; then
    apt-get install -y --no-install-recommends "$pkg"
  else
    warn "$pkg is not in the archive; skipping (Bench A needs a way to capture from the camera)"
  fi
done

step "ROS 2 apt source"
if ! dpkg -s ros2-apt-source >/dev/null 2>&1; then
  ros_apt_version="$(curl -fsSL https://api.github.com/repos/ros-infrastructure/ros-apt-source/releases/latest \
    | grep -F '"tag_name"' | awk -F'"' '{print $4}')"
  if [ -z "$ros_apt_version" ]; then
    echo "could not look up the ros-apt-source release (GitHub API unreachable or rate-limited); re-run in a minute" >&2
    exit 1
  fi
  curl -fsSL -o /tmp/ros2-apt-source.deb \
    "https://github.com/ros-infrastructure/ros-apt-source/releases/download/${ros_apt_version}/ros2-apt-source_${ros_apt_version}.${VERSION_CODENAME}_all.deb"
  dpkg -i /tmp/ros2-apt-source.deb
  rm -f /tmp/ros2-apt-source.deb
  apt-get update
else
  echo "already present"
fi

step "ROS 2 $ROS_DISTRO (ros-base: no GUI on the Pi)"
apt-get install -y --no-install-recommends \
  "ros-$ROS_DISTRO-ros-base" \
  "ros-$ROS_DISTRO-rmw-cyclonedds-cpp" \
  python3-colcon-common-extensions \
  python3-rosdep

step "rosdep"
[ -f /etc/ros/rosdep/sources.list.d/20-default.list ] || rosdep init
rosdep update --rosdistro "$ROS_DISTRO"
# The same command CI runs. The six pip-owned packages carry COLCON_IGNORE, which
# rosdep's package crawl honours, so only neo_msgs and neo_bringup count.
rosdep install --from-paths "$REPO_DIR/src" --ignore-src -y --rosdistro "$ROS_DISTRO"
# And a cache for the user, so later rosdep queries work without sudo.
sudo -u "$TARGET_USER" -H rosdep update --rosdistro "$ROS_DISTRO"

# --- device access -----------------------------------------------------------

step "device access for $TARGET_USER"
getent group i2c >/dev/null || groupadd --system i2c
for group in i2c video audio dialout gpio render; do
  if getent group "$group" >/dev/null; then
    usermod -aG "$group" "$TARGET_USER"
    echo "  + $group"
  fi
done
# /dev/i2c-* is root:i2c on Raspberry Pi OS but not reliably on Ubuntu images;
# without this the servo driver needs root.
cat > /etc/udev/rules.d/60-neo-i2c.rules <<'RULES'
SUBSYSTEM=="i2c-dev", GROUP="i2c", MODE="0660"
RULES
if have_systemd; then
  udevadm control --reload-rules || true
  udevadm trigger --subsystem-match=i2c-dev || true
fi
REBOOT_NEEDED=1  # group membership only applies to new logins

step "I2C (PCA9685 servo driver)"
CONFIG_TXT=/boot/firmware/config.txt
if [ -f "$CONFIG_TXT" ]; then
  if grep -qE '^[[:space:]]*dtparam=i2c_arm=on' "$CONFIG_TXT"; then
    echo "already enabled"
  else
    printf '\n# Neo: PCA9685 servo driver on I2C-1\ndtparam=i2c_arm=on\n' >> "$CONFIG_TXT"
    echo "enabled in $CONFIG_TXT"
    REBOOT_NEEDED=1
  fi
else
  warn "$CONFIG_TXT not found (not a Raspberry Pi boot partition); skipping"
fi

# --- network -----------------------------------------------------------------

step "mDNS, so $(hostname).local resolves from the laptop"
if have_systemd; then
  systemctl enable --now avahi-daemon
else
  warn "no systemd; avahi-daemon not started"
fi

step "Ethernet link: eth0 = $ETH_ADDRESS (DHCP kept alongside)"
sed "s#@ETH_ADDRESS@#${ETH_ADDRESS}#" "$FILES/60-neo-ethernet.yaml" > /etc/netplan/60-neo-ethernet.yaml
chmod 600 /etc/netplan/60-neo-ethernet.yaml
if command -v netplan >/dev/null && have_systemd; then
  # Generate only. `netplan apply` can bounce the interface this SSH session is
  # riding on, killing the script half-way; the reboot at the end applies it.
  netplan generate
fi
REBOOT_NEEDED=1

step "Wi-Fi power saving off"
install -m 644 "$FILES/wifi-powersave-off.service" /etc/systemd/system/wifi-powersave-off.service
if have_systemd; then
  systemctl daemon-reload
  systemctl enable wifi-powersave-off.service
fi

step "DDS pinned to lo + eth0, ROS_DOMAIN_ID=42"
install -d -m 755 /etc/neo
install -m 644 "$FILES/cyclonedds.xml" /etc/neo/cyclonedds.xml
install -m 644 "$FILES/neo-ros-env.sh" /etc/profile.d/neo-ros-env.sh

# --- optional: the panel as a service ------------------------------------------

if [ "$WITH_PANEL_SERVICE" = 1 ]; then
  step "admin panel service"
  sed -e "s#@USER@#${TARGET_USER}#" \
      -e "s#@REPO@#${REPO_DIR}#" \
      -e "s#@VENV@#${TARGET_HOME}/neo-venv#" \
      "$FILES/neo-panel.service" > /etc/systemd/system/neo-panel.service
  if have_systemd; then
    systemctl daemon-reload
    systemctl enable neo-panel.service
  fi
  echo "enabled, not started: it needs setup-user.sh and 'neo --webapp setup' first"
fi

# --- done ----------------------------------------------------------------------

step "system setup complete"
cat <<NEXT
Next, as $TARGET_USER (not root):

    bash $REPO_DIR/scripts/pi/setup-user.sh
NEXT
if [ "$REBOOT_NEEDED" = 1 ]; then
  echo
  echo "Then reboot once (sudo reboot): new groups, the I2C setting and the eth0 address all take effect on boot."
fi

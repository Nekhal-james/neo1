#!/usr/bin/env bash
# Neo: provision a Raspberry Pi 4 running Ubuntu Server 24.04 LTS (arm64).
#
# Run ON THE PI, from the copy that scripts/pi/sync-to-pi.sh put there:
#
#     sudo bash ~/neo1/scripts/pi/setup-system.sh [options]
#
#   --eth-address CIDR     static address for eth0 (default 192.168.50.2/24)
#   --with-panel-service   install and enable neo-panel.service
#   --servo-pwm            servos wired straight to the Pi: enable its hardware
#                          PWM on GPIO18/19 and let the user drive it (turns off
#                          the 3.5 mm audio jack, which shares that hardware)
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
SERVO_PWM=0
REBOOT_NEEDED=0

while [ $# -gt 0 ]; do
  case "$1" in
    --eth-address) ETH_ADDRESS="$2"; shift 2 ;;
    --with-panel-service) WITH_PANEL_SERVICE=1; shift ;;
    --servo-pwm) SERVO_PWM=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
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

step "Ubuntu package sources include ${VERSION_CODENAME}-updates"
# Some Raspberry Pi Ubuntu images ship with "Suites: noble" and noble-security
# only, while their installed libraries already come from noble-updates. apt
# then finds libzstd1 1.5.5+dfsg2-2build1.1 installed but only a -dev package
# pinned to 2build1, and every ROS install fails with "held broken packages".
# Measured on a Pi 4 flashed with 24.04.4: acl, libacl1-dev, liblz4-dev and
# libzstd-dev all refused. A standard Ubuntu install carries noble-updates.
sources=/etc/apt/sources.list.d/ubuntu.sources
updates="${VERSION_CODENAME}-updates"
if [ -f "$sources" ]; then
  if grep -qE "^Suites:.*[[:space:]]${updates}([[:space:]]|$)" "$sources"; then
    echo "already present"
  else
    cp -n "$sources" "$sources.before-neo"
    sed -i -E "s/^Suites:[[:space:]]*${VERSION_CODENAME}[[:space:]]*$/Suites: ${VERSION_CODENAME} ${updates}/" "$sources"
    if ! grep -qE "^Suites:.*[[:space:]]${updates}([[:space:]]|$)" "$sources"; then
      echo "could not add $updates to $sources (no plain 'Suites: $VERSION_CODENAME' line); add it by hand and re-run" >&2
      exit 1
    fi
    echo "added $updates (original kept as $sources.before-neo)"
  fi
elif [ -f /etc/apt/sources.list ] && ! grep -qE "^deb .*[[:space:]]${updates}[[:space:]]" /etc/apt/sources.list; then
  main_line="$(grep -m1 -E "^deb [^ ]+ ${VERSION_CODENAME} " /etc/apt/sources.list || true)"
  if [ -z "$main_line" ]; then
    echo "no '$VERSION_CODENAME' line in /etc/apt/sources.list to base $updates on; add it by hand and re-run" >&2
    exit 1
  fi
  echo "${main_line/ ${VERSION_CODENAME} / ${updates} }" >> /etc/apt/sources.list
  echo "added $updates to /etc/apt/sources.list"
else
  echo "already present"
fi

step "base packages"
apt-get update
apt-get install -y --no-install-recommends software-properties-common
add-apt-repository -y universe
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates curl git build-essential \
  python3-venv python3-dev python3-pip python3-pytest \
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

if [ "$SERVO_PWM" = 1 ]; then
  step "hardware PWM for servos on GPIO18 (pin 12) and GPIO19 (pin 35)"
  if [ ! -f "$CONFIG_TXT" ]; then
    echo "$CONFIG_TXT not found; cannot enable hardware PWM here" >&2
    exit 1
  fi
  if [ ! -f /boot/firmware/overlays/pwm-2chan.dtbo ]; then
    echo "/boot/firmware/overlays/pwm-2chan.dtbo is missing: this image has no hardware PWM overlay." >&2
    echo "Wire the servos to a PCA9685 instead (driver: pca9685 in config/motion.yaml)." >&2
    exit 1
  fi
  if grep -qE '^[[:space:]]*dtoverlay=pwm-2chan' "$CONFIG_TXT"; then
    echo "overlay already enabled"
  else
    # Its defaults are exactly the pins Neo uses: PWM0 on GPIO18, PWM1 on GPIO19.
    printf '\n[all]\n# Neo: head servo signals on the hardware PWM pins, GPIO18 and GPIO19\ndtoverlay=pwm-2chan\n' >> "$CONFIG_TXT"
    echo "enabled dtoverlay=pwm-2chan in $CONFIG_TXT"
    REBOOT_NEEDED=1
  fi
  # The 3.5 mm jack's analog audio comes from the same PWM block the servos now
  # use; the two cannot share it. Neo's speaker has to be USB.
  if grep -qE '^[[:space:]]*dtparam=audio=on' "$CONFIG_TXT"; then
    sed -i -E 's/^([[:space:]]*)dtparam=audio=on/\1dtparam=audio=off/' "$CONFIG_TXT"
    echo "turned off the 3.5 mm audio jack (dtparam=audio=off): it shares the PWM hardware"
    REBOOT_NEEDED=1
  fi
  getent group pwm >/dev/null || groupadd --system pwm
  usermod -aG pwm "$TARGET_USER"
  install -m 644 "$FILES/61-neo-pwm.rules" /etc/udev/rules.d/61-neo-pwm.rules
  if have_systemd; then
    udevadm control --reload-rules || true
  fi
  echo "  + $TARGET_USER in the pwm group; udev rule installed"
  REBOOT_NEEDED=1
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

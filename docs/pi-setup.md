# Neo — setting up the Raspberry Pi

From a blank SD card to a Pi 4 with ROS 2 Jazzy, the Neo stack installed, both
ROS packages built and every test suite passing. About an hour, most of it
unattended downloads.

The work is split in three, and the split is the point:

| Script | Runs on | As | Does |
|---|---|---|---|
| `scripts/pi/sync-to-pi.sh` | the laptop | you | copies the checkout plus the model files git does not carry |
| `scripts/pi/setup-system.sh` | the Pi | `sudo` | packages, ROS 2 Jazzy, device access, mDNS, the Ethernet link, DDS pinning |
| `scripts/pi/setup-user.sh` | the Pi | `neo`, **not** root | venv, pip install, `colcon build`, all tests |

The system/user split mirrors the one CLAUDE.md already insists on: the pip venv
and a sourced ROS never share a shell, so the user script builds with ROS in one
subshell and tests with the venv in another.

**No script handles a password, a Wi-Fi key or a certificate.** Those steps are
marked below and stay yours.

---

## 1. Flash Ubuntu Server 24.04 LTS

It has to be **24.04** (noble). ROS 2 Jazzy is only packaged for it; the setup
script refuses to run on anything else rather than half-install.

In Raspberry Pi Imager: *Other general-purpose OS → Ubuntu → Ubuntu Server
24.04.x LTS (64-bit)*. Then edit the OS customisation settings — this is where
most of the manual work disappears:

| Setting | Value | Why |
|---|---|---|
| Hostname | `neo-pi` | the name every doc and script assumes |
| Username / password | `neo` / your choice | the password is only for `sudo` from now on |
| Wi-Fi | your network, with the right country | **the Pi's only internet** — the cable to the laptop has none |
| Timezone | yours | |
| SSH | enabled, **public-key authentication only** | |
| Authorized keys | the contents of `~/.ssh/id_ed25519_neo.pub` | so nothing ever types a password over SSH |

Print the key to paste with:

```bash
cat ~/.ssh/id_ed25519_neo.pub
```

If that file does not exist yet, create it first:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_neo -C neo-robot
```

First boot runs cloud-init and reboots once. Give it a few minutes.

> **No Wi-Fi available?** Share the laptop's connection over the cable instead:
> on Windows, *Network Connections → your Wi-Fi adapter → Properties → Sharing →
> allow other users to connect*, choosing the Ethernet adapter. Windows then
> gives its Ethernet port 192.168.137.1 and hands the Pi an address by DHCP. The
> Pi keeps its own static address alongside it (§3), so nothing else changes.

## 2. Reach the Pi

```bash
ssh -i ~/.ssh/id_ed25519_neo neo@neo-pi.local
```

On a brand-new install that name **will not resolve yet**: the Ubuntu image has
no mDNS responder, and `setup-system.sh` is what installs one. Over Wi-Fi, use
the address your router gave it. Over the direct cable there is no IPv4 at all
yet, only the automatic IPv6 link-local address — find it from Windows
PowerShell:

```powershell
$eth = Get-NetAdapter -Name 'Ethernet'
ping -6 -n 2 "ff02::1%$($eth.ifIndex)" | Out-Null
Get-NetNeighbor -InterfaceIndex $eth.ifIndex |
  Where-Object LinkLayerAddress -match '^(2c-cf-67|dc-a6-32|e4-5f-01|d8-3a-dd|b8-27-eb|28-cd-c1)'
```

That lists the Pi by its Raspberry Pi MAC prefix. SSH to the `fe80::…` address
with the interface number appended, for example
`ssh -i ~/.ssh/id_ed25519_neo neo@fe80::2ecf:67ff:fe66:776a%25`. Because it is
derived from the MAC, the address normally survives a reflash.

## 3. Copy the checkout and run the setup

On the laptop, from the repo root. The dry run first — it lists what would be
sent and fails if a certificate or a `*.local.yaml` would be among it:

```bash
bash scripts/pi/sync-to-pi.sh --dry-run
bash scripts/pi/sync-to-pi.sh neo@neo-pi.local        # or the fe80::… address
```

It sends the models too (`models/`, `yolov8n-pose.pt`, about 170 MB), so the Pi
never has to download them.

Then on the Pi:

```bash
sudo bash ~/neo1/scripts/pi/setup-system.sh     # you type the sudo password
sudo reboot
bash ~/neo1/scripts/pi/setup-user.sh
```

`setup-system.sh` accepts `--eth-address 192.168.50.2/24` to change the static
address, `--with-panel-service` and `--with-robot-service` to install the admin
panel and the robot's ROS graph as systemd units that start on boot (§6), and
`--servo-pwm` when the servos' signal wires go
straight to the Pi's hardware PWM pins (see [hardware.md](hardware.md); it turns
off the 3.5 mm audio jack). Both scripts are idempotent: after a failure, fix
the cause and re-run. After syncing a newer checkout, re-run `setup-user.sh`.

What the system script sets up, and why each one:

| | |
|---|---|
| ROS 2 Jazzy `ros-base` + CycloneDDS | no desktop on a headless Pi |
| `rosdep install` over `src/` | the command CI runs; installs what the nodes import (`cv_bridge`, `vision_msgs`, ...). `neo_webapp` carries `COLCON_IGNORE` and is skipped |
| `neo` in `i2c`, `video`, `audio`, `dialout` | servo driver, camera and mic without root |
| `dtparam=i2c_arm=on` | the PCA9685 lives on I2C-1 ([wiring](hardware.md)) |
| `noble-updates` in the apt sources | some Pi images ship without it, and then ROS cannot install |
| `avahi-daemon` | makes `neo-pi.local` resolve from the laptop and your phone |
| eth0 = `192.168.50.2/24`, DHCP kept | the plan's fixed address for the laptop link (plan Phase 0, step 4) |
| Wi-Fi power saving off | power-save naps look like a flapping failover path |
| `/etc/neo/cyclonedds.xml`, `ROS_DOMAIN_ID=42` | DDS discovery pinned to `lo` and `eth0`, so campus Wi-Fi's other ROS machines never find the robot |

It **generates** the network config but does not apply it, because
`netplan apply` can drop the very SSH session the script is running in. The
reboot applies it.

`setup-user.sh` also writes `config/intelligence.local.yaml` (model paths),
`config/model_conn.local.yaml` (the laptop at `192.168.50.1`) and
`config/audio.local.yaml` (the wake word export it finds in `models/wakeword/`)
— but only when they do not exist, so it never overwrites a file you have
edited.

**The wake word model is yours, not a download.** Train it in Teachable Machine
as an audio project, export it as TensorFlow.js, and put the `.zip` in
`models/wakeword/` on the laptop before syncing (or copy it straight to
`~/neo1/models/wakeword/` on the Pi). How to train it so ordinary speech does not
fire it: [src/neo_audio/README.md](../src/neo_audio/README.md).

## 4. The steps that stay yours

Each of these needs a password, a key or a secret, which is why no script does
them.

1. **The laptop's end of the cable** — give its Ethernet adapter the static
   address `192.168.50.1/24` (*Network Connections → Ethernet → IPv4 →
   Properties*). Skip this if you are using connection sharing, and change
   `config/model_conn.local.yaml` on the Pi to `192.168.137.1` instead.
2. **The admin panel password** — on the Pi: `~/neo-venv/bin/neo --webapp setup`.
   It prints a one-time recovery code for the sign-in page's **Forgot
   password?**; save it somewhere off the robot.
3. **Certificates** — follow [docs/security.md](security.md). Issue the panel
   certificate **on the Pi, after the reboot**, so it covers `neo-pi.local` and
   the `192.168.50.2` address that only exists once the network config is live:
   `~/neo-venv/bin/neo --tls panel`.
4. **mTLS for the link** — `tls.enabled: true` in `config/model_conn.local.yaml`
   on both machines, once the certificates are in place.

## 5. Check it

On the Pi:

```bash
# The ROS side (in a shell with ROS sourced, not the venv):
source /opt/ros/jazzy/setup.bash && source ~/neo1/install/setup.bash
ros2 interface show neo_msgs/msg/HeadCommand

# Hardware, once it is wired:
i2cdetect -y 1                  # the PCA9685 answers at 0x40
~/neo-venv/bin/neo-servo-check  # read-only; wiring and the next steps: docs/hardware.md
lsusb                           # the USB webcam/mic must be listed here first
v4l2-ctl --list-devices         # a USB camera adds its own /dev/video*, beside the bcm2835 codec nodes
arecord -l                      # the USB mic
~/neo-venv/bin/neo-audio-check  # devices, wake word model, speech; then --listen, --say "hello"

# The link to the laptop:
~/neo-venv/bin/neo --connection:status
```

From the laptop or your phone, once the panel is running on the Pi:
`https://neo-pi.local:8443`.

## 6. Run the robot

```bash
bash ~/neo1/scripts/pi/neo-up.sh --check     # what would start, and whether it imports
bash ~/neo1/scripts/pi/neo-up.sh --panel     # the ROS graph plus the admin panel
```

`--profile dev` runs with no hardware at all; ctrl-C stops the graph (the panel,
started with `--panel`, keeps running — its pid is in `var/panel.pid`). To start
both at every boot:

```bash
sudo bash ~/neo1/scripts/pi/setup-system.sh --with-robot-service --with-panel-service
sudo systemctl start neo-robot neo-panel
journalctl -u neo-robot -f                   # the graph's log
```

Always through these scripts, never `ros2 launch` or `neo --webapp up` by hand:
the robot script makes the venv's speech packages importable by ROS's own
interpreter, and the panel script keeps the panel on the robot's DDS domain.
Get either wrong and nothing errors — the voice nodes report a package as "not
installed" that is, or the panel shows `backend: mock` and controls a simulated
robot. The header badge says `backend: ros` when it is right.

## What this deliberately does not do

- **The Phase 0 benchmarks** (camera, YOLO, Piper, wake word, Vosk, off-board
  LLM). They measure the hardware and belong in `docs/hardware.md`; the setup
  only makes them runnable.
- **Start the robot.** Setup ends with the services installed at most, never
  started: the first start is yours, after the steps in §4 (§6).
- **Export YOLO to NCNN** for the Pi (Bench B). The PyTorch model runs as
  installed; NCNN is the optimisation once the benchmark says it is needed.

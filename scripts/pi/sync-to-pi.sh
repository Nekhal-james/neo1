#!/usr/bin/env bash
# Copy this checkout to the Pi: the code, plus the model files git does not carry.
#
#     bash scripts/pi/sync-to-pi.sh [--dry-run] [user@host]    # default: neo@neo-pi.local
#
# Runs from Git Bash, WSL, macOS or Linux -- plain tar over ssh, nothing extra to
# install. Uses ~/.ssh/id_ed25519_neo when it exists (override with NEO_SSH_KEY).
# --dry-run lists what would be sent and checks no secret is in it, without
# touching the network.
#
# Deliberately NOT copied:
#   certs/, config/*.local.yaml   secrets and per-machine settings; certificates
#                                 move per docs/security.md, deliberately
#   .git, build/, install/, log/  history and build output -- the Pi builds its
#   var/, caches, *.egg-info      own, for its own architecture
#   src/intelligence/rag/data/*.yaml
#                                 campus data is edited on the Pi's panel; a
#                                 sync must never overwrite it with the laptop's
#
# Re-run it to update the Pi. It adds and overwrites files; it never deletes
# something removed on the laptop.

set -euo pipefail

DRY_RUN=0
if [ "${1:-}" = "--dry-run" ]; then
  DRY_RUN=1
  shift
fi
TARGET="${1:-neo@10.220.142.170}"
KEY="${NEO_SSH_KEY:-$HOME/.ssh/id_ed25519_neo}"
REMOTE_DIR="${NEO_REMOTE_DIR:-neo1}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

TAR_ARGS=(
  -C "$REPO_DIR" -czf -
  --exclude=./.git
  --exclude=./build --exclude=./install --exclude=./log
  --exclude=./var --exclude=./certs
  --exclude='./config/*.local.yaml'
  --exclude='./src/intelligence/rag/data/*.yaml'
  --exclude='*.egg-info' --exclude=__pycache__
  --exclude=.pytest_cache --exclude=.ruff_cache
  .
)

for path in models yolov8n-pose.pt; do
  [ -e "$REPO_DIR/$path" ] || echo "note: $path is missing on this machine, so the Pi will not get it either"
done

if [ "$DRY_RUN" = 1 ]; then
  listing="$(tar "${TAR_ARGS[@]}" | tar -tzvf -)"
  echo "==> dry run: nothing sent"
  echo "$listing" | awk '{ n++; size += $3 } END { printf "    %d entries, %.1f MB before compression\n", n, size / 1048576 }'
  echo "    top level:"
  echo "$listing" | awk '{ print $NF }' | awk -F/ 'NF > 1 && $2 != "" { print "      " $2 }' | sort -u
  leaked="$(echo "$listing" | awk '{ print $NF }' | grep -E '^\./(certs/|config/[^/]*\.local\.yaml$|\.git/)' || true)"
  if [ -n "$leaked" ]; then
    echo "ERROR: would copy something that must stay on this machine:" >&2
    echo "$leaked" >&2
    exit 1
  fi
  echo "    no certs/, no *.local.yaml, no .git: ok"
  exit 0
fi

SSH=(ssh -o BatchMode=yes -o ConnectTimeout=10)
[ -f "$KEY" ] && SSH+=(-i "$KEY")

echo "==> checking $TARGET (key auth only, never prompts)"
if ! "${SSH[@]}" "$TARGET" true; then
  echo "cannot reach $TARGET with key authentication -- see docs/pi-setup.md" >&2
  exit 1
fi

echo "==> copying $REPO_DIR to $TARGET:~/$REMOTE_DIR"
tar "${TAR_ARGS[@]}" | "${SSH[@]}" "$TARGET" "mkdir -p ~/$REMOTE_DIR && tar -xzf - -C ~/$REMOTE_DIR"

echo "==> done. On the Pi:"
echo "    sudo bash ~/$REMOTE_DIR/scripts/pi/setup-system.sh"
echo "    sudo reboot"
echo "    bash ~/$REMOTE_DIR/scripts/pi/setup-user.sh"

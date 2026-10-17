#!/usr/bin/env bash
# Copies the host's ~/.ssh (bind-mounted read-only at ~/.ssh-host, see
# devcontainer.json "mounts") into the container's ~/.ssh, so that `ssh pi_a`
# / `scp` work with the host's config and keys. A plain bind mount of ~/.ssh is
# avoided because OpenSSH rejects config/keys whose owner or mode look wrong,
# and a read-only ~/.ssh cannot record new known_hosts entries.
# Runs on every container start (postStartCommand), so host-side changes are
# picked up by restarting the container; no rebuild needed.
set -euo pipefail

SRC=/home/vscode/.ssh-host
DST=/home/vscode/.ssh

if [ ! -d "$SRC" ]; then
  echo "sync-host-ssh.sh: $SRC not mounted, skipping" >&2
  exit 0
fi

mkdir -p "$DST"
chmod 700 "$DST"
# Regular files only: skips ControlMaster sockets and other special files.
(cd "$SRC" && find . -type f -exec cp --parents -f {} "$DST"/ \;)
find "$DST" -type d -exec chmod 700 {} +
find "$DST" -type f -exec chmod 600 {} +
find "$DST" -type f -name '*.pub' -exec chmod 644 {} +

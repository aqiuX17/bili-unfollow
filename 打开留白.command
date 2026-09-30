#!/bin/zsh
set -eu
TASK_URL='http://127.0.0.1:22330'
TASK_SSH_HOST=${BILI_SSH_HOST:-bili-unfollow-server}
if ! curl -fsS --max-time 3 "$TASK_URL/" 2>/dev/null | grep -q '<title>留白'; then
  ssh -fN -o BatchMode=yes -o ConnectTimeout=10 -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    -L 127.0.0.1:22330:127.0.0.1:22330 "$TASK_SSH_HOST"
fi
open "$TASK_URL/"

#!/bin/bash
# Start (or check) a long-running `claude remote-control` session on ORCD.
# Run ON the cluster (a login node is fine). See ../../references/remote-control.md.
#
#   claude-rc-submit.sh check                     prerequisites + partitions you can use
#   claude-rc-submit.sh status                    your claude-rc jobs and the latest log lines
#   claude-rc-submit.sh start [-d DEADLINE] -p PARTITION [-c CPUS] [-m MEM] [-w WORKDIR]
#
# DEADLINE is anything `date -d` reads ("2026-12-20 09:00", "+14 days"); default +14 days.
# State and logs go to $RC_DIR (default: a claude-rc folder in ~/orcd/scratch), never $HOME.
set -eo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
export PATH=$HOME/.local/bin:$PATH
SCR=$(readlink -f ~/orcd/scratch 2>/dev/null || true)
RC_DIR=${RC_DIR:-${SCR:+$SCR/claude-rc}}
MIN_PART=43200   # MaxTime >= 12 h: each successor starts 2 h early and waits, so shorter runs waste their slots

partitions() {   # partitions this user may submit a 2-CPU job to, with MaxTime and a note
  scontrol show partition -o 2>/dev/null | sed -E 's/^PartitionName=([^ ]+).* MaxTime=([^ ]+).*/\1 \2/' |
    while read -r p t; do
      sbatch --test-only -p "$p" -t 60 -c 2 --mem=4G --wrap=true >/dev/null 2>&1 || continue
      case "$p" in
        *preempt*|*data_transfer*) echo "$p $t   (avoid: preemptable or transfer-only)" ;;
        *) if [ "$(seconds "$t")" -lt "$MIN_PART" ]; then echo "$p $t   (too short for the chain)"; else echo "$p $t"; fi ;;
      esac
    done || true
}

seconds() {     # Slurm time (D-HH:MM:SS, HH:MM:SS, UNLIMITED) -> seconds
  case "$1" in
    UNLIMITED|infinite) echo 2592000 ;;
    *-*) echo $(( ${1%%-*} * 86400 + $(echo "${1#*-}" | awk -F: '{print $1*3600+$2*60+$3}') )) ;;
    *) echo "$1" | awk -F: '{ if (NF==3) print $1*3600+$2*60+$3; else if (NF==2) print $1*60+$2; else print $1*60 }' ;;
  esac
}

check() {
  local ok=0
  command -v sbatch >/dev/null || { echo "✗ not on ORCD (no sbatch). Run this on the cluster."; return 1; }
  if command -v claude >/dev/null; then echo "✓ Claude Code $(claude --version 2>/dev/null | head -1)"
  else echo "✗ Claude Code not found. Next: curl -fsSL https://claude.ai/install.sh | bash   (installs to ~/.local/bin)"; ok=1; fi
  if [ -s ~/.claude/.credentials.json ]; then echo "✓ Claude sign-in found"
  else echo "✗ No Claude sign-in. Next: run \`claude\` once in this terminal and sign in with your lab Claude account, then /exit"; ok=1; fi
  [ -n "${ANTHROPIC_API_KEY:-}" ] && echo "· ANTHROPIC_API_KEY is set: remote control needs the claude.ai sign-in, and jobs would inherit the key; unset it before start"
  if [ -n "$RC_DIR" ]; then echo "✓ state and logs: $RC_DIR"
  else echo "✗ ~/orcd/scratch is missing. Next: set RC_DIR to a folder on your scratch (not \$HOME)"; ok=1; fi
  echo "· partitions you can use (MaxTime):"
  partitions | sed 's/^/    /' || true
  return $ok
}

status() {
  squeue -u "$USER" -n claude-rc -o '%i %T %P %M %L %S %N'
  local last
  last=$(ls -t "$RC_DIR"/claude-rc-*.out 2>/dev/null | head -1 || true)
  [ -n "$last" ] && { echo "--- $last"; tail -n 8 "$last" | sed -E "s/\x1b\[[0-9;?]*[A-Za-z]//g"; }
  return 0
}

start() {
  local deadline="+14 days" part="" cpus=2 mem=16G work="$SCR" opt
  while getopts "d:p:c:m:w:" opt; do
    case $opt in d) deadline=$OPTARG ;; p) part=$OPTARG ;; c) cpus=$OPTARG ;; m) mem=$OPTARG ;; w) work=$OPTARG ;; *) exit 2 ;; esac
  done
  [ -n "$part" ] || { echo "Pick a partition with -p (see: $0 check)"; exit 2; }
  [ -n "$RC_DIR" ] || { echo "~/orcd/scratch is missing; set RC_DIR to a folder on your scratch"; exit 2; }
  [ -n "$work" ] || { echo "No folder to serve: ~/orcd/scratch is missing; pass -w <folder>"; exit 2; }
  local given=$work home
  work=$(readlink -f "$work" || true); RC_DIR=$(readlink -m "$RC_DIR")
  [ -n "$work" ] && [ -d "$work" ] || { echo "No such folder: $given"; exit 2; }
  home=$(readlink -f "$HOME")
  case "$home/" in "${work%/}/"*) echo "Don't serve your home folder or a folder above it (it holds your keys and sign-ins); pick a folder on scratch"; exit 2 ;; esac
  case "$work$RC_DIR$mem$cpus" in *,*) echo "Folders and sizes can't contain commas"; exit 2 ;; esac
  [ -z "${ANTHROPIC_API_KEY:-}" ] || { echo "Unset ANTHROPIC_API_KEY first: remote control uses your claude.ai sign-in, and jobs would inherit the key"; exit 2; }
  check >/dev/null || { echo "Prerequisites missing; run: $0 check"; exit 1; }
  if squeue -h -u "$USER" -n claude-rc -o %i | grep -q .; then
    echo "You already have claude-rc jobs (cancel them first with scancel -n claude-rc):"; status; exit 1
  fi
  local D T MAX L
  D=$(date -d "$deadline" +%s)
  [ "$D" -gt "$(date +%s)" ] || { echo "Deadline is in the past: $deadline"; exit 2; }
  T=$(scontrol show partition "$part" | grep -oE "MaxTime=[^ ]+" | cut -d= -f2)
  MAX=$(seconds "$T")
  [ "$MAX" -ge "$MIN_PART" ] || { echo "Partition $part allows only $T per job; the chain needs at least 12 hours. Pick another (see: $0 check)."; exit 2; }
  [ "$MAX" -le 172800 ] || MAX=172800
  mkdir -p "$RC_DIR"; chmod 700 "$RC_DIR"
  install -m 700 "$HERE/claude-rc.sbatch" "$RC_DIR/claude-rc.sbatch"   # successors use this copy, so skill updates don't break the chain
  L=$(RC_DIR=$RC_DIR RC_DEADLINE=$D RC_MAX_RUN=$MAX bash "$RC_DIR/claude-rc.sbatch" limit)
  [ "$L" -ge 30 ] || { echo "Less than 30 minutes before the deadline or a maintenance window; pick a later deadline."; exit 2; }
  echo "partition $part (MaxTime $T), deadline $(date -Is -d @"$D"), first run ${L} min, serving $work"
  echo "(the folder must be trusted: run \`claude\` there once and answer Yes, or the session waits at that prompt)"
  sbatch -p "$part" -t "$L" -c "$cpus" --mem="$mem" -o "$RC_DIR/%x-%j.out" \
    --export="ALL,RC_DEADLINE=$D,RC_PARTITION=$part,RC_MAX_RUN=$MAX,RC_CPUS=$cpus,RC_MEM=$mem,RC_DIR=$RC_DIR,RC_WORKDIR=$work" \
    "$RC_DIR/claude-rc.sbatch"
}

case "${1:-}" in
  check) check ;;
  status) status ;;
  start) shift; start "$@" ;;
  *) sed -n '2,10p' "$0"; exit 2 ;;
esac

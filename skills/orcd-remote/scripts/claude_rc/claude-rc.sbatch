#!/bin/bash
#SBATCH -J claude-rc
#SBATCH --requeue
#SBATCH --open-mode=append
#SBATCH --signal=B:USR1@600
# Keeps `claude remote-control` running on ORCD until a deadline, as a chain of
# Slurm jobs: each run submits its successor before its time limit, steps around
# the monthly maintenance window, and hands over when the predecessor exits.
# Submit with claude-rc-submit.sh (it sets partition, resources, log path and the
# RC_* variables below); don't sbatch this file directly.
#   RC_DEADLINE   epoch seconds to stop serving (required)
#   RC_DIR        state and logs (default: a claude-rc folder in ~/orcd/scratch)
#   RC_WORKDIR    folder remote control serves (default ~/orcd/scratch)
#   RC_PARTITION, RC_MAX_RUN, RC_CPUS, RC_MEM   carried to successors
# `RC_DEADLINE=<epoch> bash claude-rc.sbatch limit` prints the first run's minutes;
# `RC_DEADLINE=<epoch> bash claude-rc.sbatch plan <run end epoch>` prints the successor's "begin minutes".

umask 077
: "${RC_DEADLINE:?submit with --export=ALL,RC_DEADLINE=<epoch seconds>}"
deadline=$RC_DEADLINE
MAX_RUN=${RC_MAX_RUN:-172800}
RC_PARTITION=${RC_PARTITION:-${SLURM_JOB_PARTITION:-}}
RC_CPUS=${RC_CPUS:-2}
RC_MEM=${RC_MEM:-16G}
LEAD=7200
RC_DIR=${RC_DIR:-$(readlink -f ~/orcd/scratch)/claude-rc}
RC_WORKDIR=${RC_WORKDIR:-$(readlink -f ~/orcd/scratch)}
RC_SCRIPT=$RC_DIR/claude-rc.sbatch
RC_SUB=(-p "$RC_PARTITION" -c "$RC_CPUS" --mem="$RC_MEM" -o "$RC_DIR/%x-%j.out")
RC_ENV="ALL,RC_DEADLINE=$deadline,RC_PARTITION=$RC_PARTITION,RC_MAX_RUN=$MAX_RUN,RC_CPUS=$RC_CPUS,RC_MEM=$RC_MEM,RC_DIR=$RC_DIR,RC_WORKDIR=$RC_WORKDIR"
export PATH=$HOME/.local/bin:$PATH

third_tuesday() {
  local dow
  dow=$(date -d "$1" +%u)
  date -d "$1 +$(( (9 - dow) % 7 + 14 )) days" +%s
}

maint_window() {
  local now=$1 win t
  win=$(scontrol show reservation -o 2>/dev/null \
    | awk '/Flags=[^ ]*MAINT/ && /ALL_NODES/ { s=e=""; for (i=1;i<=NF;i++) { if ($i ~ /^StartTime=/) s=substr($i,11); if ($i ~ /^EndTime=/) e=substr($i,9) } print s, e }' \
    | while read -r s e; do echo "$(date -d "$s" +%s) $(date -d "$e" +%s)"; done \
    | awk -v now="$now" '$2 > now' | sort -n | head -1)
  if [ -z "$win" ]; then
    t=$(third_tuesday "$(date -d @"$now" +%Y-%m-01)")
    [ $(( t + 86400 )) -le "$now" ] && t=$(third_tuesday "$(date -d "$(date -d @"$now" +%Y-%m-01) +1 month" +%Y-%m-01)")
    win="$t $(( t + 86400 ))"
  fi
  echo "$win"
}

run_minutes() {
  local start=$1 ms me end
  read -r ms me < <(maint_window "$start")
  end=$deadline
  [ $(( start + MAX_RUN )) -lt "$end" ] && end=$(( start + MAX_RUN ))
  [ "$start" -lt "$ms" ] && [ $(( ms - 300 )) -lt "$end" ] && end=$(( ms - 300 ))
  echo $(( (end - start) / 60 ))
}

first_limit() {
  local now ms me start
  now=${RC_NOW:-$(date +%s)}
  read -r ms me < <(maint_window "$now")
  start=$now
  [ $(( ms - now )) -lt 3600 ] && [ "$me" -gt "$now" ] && start=$me
  run_minutes "$start"
}

plan_next() {
  local e=$1 now ms me begin
  now=${RC_NOW:-$(date +%s)}
  read -r ms me < <(maint_window "$e")
  if [ $(( ms - e )) -lt 3600 ] && [ "$me" -gt "$e" ]; then
    begin=$me
  else
    begin=$(( e - LEAD ))
    [ "$begin" -lt "$now" ] && begin=$now
  fi
  echo "$begin $(run_minutes "$begin")"
}

chain_successor() {
  local marker=$RC_DIR/.chained-$SLURM_JOB_ID e begin mins
  [ -e "$marker" ] && return 0
  e=$(date -d "$(squeue -h -j "$SLURM_JOB_ID" -o %e)" +%s) || return 1
  read -r begin mins < <(plan_next "$e")
  if [ "$mins" -lt $(( LEAD / 60 + 30 )) ] || [ "$begin" -ge "$deadline" ]; then
    echo "$(date -Is) no successor: deadline $(date -Is -d @"$deadline") is within this run"
    touch "$marker"; return 0
  fi
  echo "$(date -Is) submitting successor: begin $(date -Is -d @"$begin"), ${mins} min"
  sbatch "${RC_SUB[@]}" -t "$mins" --begin="$(date -d @"$begin" +%Y-%m-%dT%H:%M:%S)" \
    --export="$RC_ENV,RC_PRED=$SLURM_JOB_ID" "$RC_SCRIPT" && touch "$marker"
}

case "${1:-}" in
  limit) first_limit; exit 0 ;;
  plan) plan_next "$2"; exit 0 ;;
esac

LOG=$RC_DIR/claude-rc-$SLURM_JOB_ID.out
cd "$RC_WORKDIR"
echo "$(date -Is) start job=$SLURM_JOB_ID restart=${SLURM_RESTART_COUNT:-0} node=$(hostname) deadline=$(date -Is -d @"$deadline") pred=${RC_PRED:-none}"
echo "$(date -Is) claude update: $(timeout 300 claude update 2>&1 | grep -aE 'Current version|Successfully|up to date|rror' | tr '\n' ' ')"
echo "$(date -Is) claude=$(claude --version)"

if [ -n "${RC_PRED:-}" ]; then
  echo "$(date -Is) waiting for predecessor $RC_PRED to exit"
  gone=0
  while [ "$gone" -lt 2 ]; do
    if squeue -h -j "$RC_PRED" -o %T 2>/dev/null | grep -qvx COMPLETING; then gone=0; else gone=$(( gone + 1 )); fi
    sleep 5
  done
  echo "$(date -Is) predecessor gone"
fi

reap_job() {
  local pids sig
  for sig in TERM KILL; do
    pids=$(scontrol listpids "$SLURM_JOB_ID" 2>/dev/null | awk -v self="$$" 'NR > 1 && $1 != self {print $1}')
    [ -n "$pids" ] || return 0
    echo "$(date -Is) reaping $(echo $pids | wc -w) leftover processes with SIG$sig"
    kill -"$sig" $pids 2>/dev/null
    [ "$sig" = TERM ] && sleep 15
  done
}

on_usr1() {
  kill "$child" 2>/dev/null; wait "$child"
  if [ ! -e "$RC_DIR/.chained-$SLURM_JOB_ID" ]; then
    local mins
    mins=$(first_limit)
    if [ "$mins" -ge 30 ]; then
      echo "$(date -Is) no successor was pre-submitted; submitting one after this job"
      sbatch "${RC_SUB[@]}" -t "$mins" --dependency=afterany:"$SLURM_JOB_ID" --export="$RC_ENV" "$RC_SCRIPT"
    fi
  fi
  echo "$(date -Is) run ending"
  reap_job
  exit 0
}
trap on_usr1 USR1

for attempt in $(seq 1 20); do
  remaining=$(( deadline - $(date +%s) ))
  [ "$remaining" -gt 0 ] || { echo "$(date -Is) deadline passed"; exit 0; }
  t0=$(date +%s)
  timeout "${remaining}s" script -qefc "claude remote-control" /dev/null &
  child=$!
  ( sleep 90; kill -0 "$child" 2>/dev/null && chain_successor ) &
  wait "$child"; rc=$?
  if [ $(( $(date +%s) - t0 )) -lt 90 ] && tail -c 4000 "$LOG" | grep -aq "already served"; then
    echo "$(date -Is) folder still served, retry $attempt"
    sleep 30; continue
  fi
  break
done
echo "$(date -Is) claude remote-control exited rc=$rc"
reap_job

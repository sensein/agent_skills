#!/bin/bash
# colab-bus: a light, self-requeuing Slurm job that polls GitHub.
#
# Copy this template OUTSIDE git (e.g. {{STATE_DIR}}/colab-bus.local.sbatch), fill the
# placeholders there, then: sbatch {{STATE_DIR}}/colab-bus.local.sbatch
#   {{PARTITION}}   a partition you may use for long, tiny jobs
#   {{STATE_DIR}}   private group storage, NOT scratch (purged); must exist before sbatch
#   {{META_CLONE}}  a clone of the meta repo; the bus pulls it every poll
#   {{PYTHON}}      a Python 3.11+ interpreter (cluster python3 may be older), e.g. a uv-managed one
#   {{TOKEN_FILE}}  0600 file with a fine-grained token or GitHub App token (see SKILL.md)
#SBATCH -J colab-bus
#SBATCH -p {{PARTITION}}
#SBATCH -c 1
#SBATCH --mem=1G
#SBATCH -t 1-00:00:00
#SBATCH --requeue
#SBATCH --open-mode=append
#SBATCH --signal=B:USR1@900
#SBATCH -o {{STATE_DIR}}/colab-bus-%j.out

umask 077
# With [security] signing = "ssh": the bus signs COLAB_SECURITY_STATE with this private key (0600, on this host
# only, never in git); its public key goes in the meta repo's .colab/allowed_signers as `colab-bus <pubkey>`.
# export COLAB_SIGNING_KEY={{STATE_DIR}}/colab-security-signing-key
STATE_DIR={{STATE_DIR}}
META={{META_CLONE}}
PY={{PYTHON}}

requeue() {
  echo "$(date -Is) requeueing $SLURM_JOB_ID ($1)"
  scontrol requeue "$SLURM_JOB_ID"
  exit 0
}

# Near the time limit Slurm sends USR1: let the bus finish its poll, then requeue.
on_usr1() {
  kill -USR1 "$child" 2>/dev/null; wait "$child"
  requeue "time limit"
}
trap on_usr1 USR1

# Pull before starting: the bus imports its helper code once, at startup.
git -C "$META" pull --ff-only -q || echo "$(date -Is) meta pull failed; using the last checkout"
echo "$(date -Is) start job=$SLURM_JOB_ID restart=${SLURM_RESTART_COUNT:-0} node=$(hostname)"
"$PY" "$META/bus/colab_bus.py" --meta "$META" --state-dir "$STATE_DIR" --token-file {{TOKEN_FILE}} &
child=$!
wait "$child"
rc=$?
echo "$(date -Is) bus exited rc=$rc"
# An unexpected exit also requeues, after a pause, so one bad poll never ends the bus.
if [ "$rc" -ne 0 ]; then
  sleep 300
  requeue "exit $rc"
fi

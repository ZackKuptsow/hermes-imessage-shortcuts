#!/usr/bin/env bash
# n8n/watch-egress.sh: samples every socket the `_n8n` user holds, every 2 seconds, into a
# log in the home of whoever ran it, for the egress check in n8n/README.md. Run with sudo
# in a terminal of its own after install.sh has installed n8n and before n8n's first start,
# and leave it a day; Ctrl-C stops it. install.sh refuses that first start until it runs.
# A sample is written only when it differs from the one before, so a quiet day stays small;
# a line every hour and the last line count the samples, so an empty record cannot pass for
# a watch that never ran. It records what it sees and judges nothing: the README's check
# reads the record. Sampling misses a connection that opens and closes between two samples,
# which is why the packet-filter anchor's counters are the lasting proof.
set -uo pipefail

N8N_USER="_n8n"
DAEMON_LABEL="com.zack.hermes-imessage.n8n"
# install.sh reads the same path to tell that a watch is running.
EGRESS_WATCH_PIDFILE="/var/run/${DAEMON_LABEL}.egress-watch.pid"
LOG_NAME="n8n-egress-watch.log"
SAMPLE_INTERVAL_SECONDS=2
SAMPLES_PER_COUNT_LINE=1800  # an hour, at one sample every 2 seconds
WATCH_EXIT_REFUSED=3

samples=0

say() {
  printf 'n8n egress watch: %s\n' "$*"
}

refuse() {
  printf 'n8n egress watch: %s\n' "$*" >&2
  exit "$WATCH_EXIT_REFUSED"
}

timestamp() {
  date '+%Y-%m-%dT%H:%M:%S%z'
}

check_preconditions() {
  local running_pid
  [[ "$EUID" -eq 0 ]] || refuse "run this with sudo: lsof shows another user's sockets to root alone"
  [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != root ]] \
    || refuse "run this with sudo from your own account; the record goes in your home"
  id -u "$N8N_USER" >/dev/null 2>&1 || refuse "${N8N_USER} does not exist yet; run n8n/install.sh first"
  if [[ -f "$EGRESS_WATCH_PIDFILE" ]]; then
    running_pid="$(cat "$EGRESS_WATCH_PIDFILE")"
    if kill -0 "$running_pid" 2>/dev/null; then
      refuse "a watch is already running, as PID ${running_pid}"
    fi
  fi
}

# record_path: the record's path in the sudo user's home, made there and owned by that user.
record_path() {
  local home path
  home="$(dscl . -read "/Users/${SUDO_USER}" NFSHomeDirectory | awk '{ print $2 }')"
  [[ -d "$home" ]] || refuse "cannot find the home of ${SUDO_USER}"
  path="${home}/${LOG_NAME}"
  { touch "$path" && chown "$SUDO_USER" "$path"; } || refuse "cannot write ${path}"
  printf '%s\n' "$path"
}

stop() {
  printf '%s stopped after %s samples\n' "$(timestamp)" "$samples" >>"$record"
  rm -f "$EGRESS_WATCH_PIDFILE" "$lsof_errors"
  say "stopped after ${samples} samples; the record is ${record}"
}

watch() {
  local current previous="(not sampled yet)"
  while true; do
    current="$(lsof -nP -i -a -u "$N8N_USER" 2>"$lsof_errors")"
    samples=$((samples + 1))
    # lsof exits 1 both when it finds nothing and when it fails; only stderr tells them apart.
    if [[ -s "$lsof_errors" ]]; then
      { printf '%s sample %s: lsof failed:\n' "$(timestamp)" "$samples"; cat "$lsof_errors"; } >>"$record"
    elif [[ "$current" != "$previous" ]]; then
      printf '== %s sample %s\n%s\n' "$(timestamp)" "$samples" "${current:-(no sockets)}" >>"$record"
      previous="$current"
    fi
    if (( samples % SAMPLES_PER_COUNT_LINE == 0 )); then
      printf '%s %s samples so far\n' "$(timestamp)" "$samples" >>"$record"
    fi
    sleep "$SAMPLE_INTERVAL_SECONDS"
  done
}

main() {
  check_preconditions
  record="$(record_path)" || exit "$WATCH_EXIT_REFUSED"
  lsof_errors="$(mktemp)" || refuse "cannot make a scratch file for lsof's errors"
  printf '%s\n' "$$" >"$EGRESS_WATCH_PIDFILE" || refuse "cannot write ${EGRESS_WATCH_PIDFILE}"
  trap stop EXIT
  trap 'exit 0' INT TERM
  printf '%s started: lsof -nP -i -a -u %s every %ss\n' "$(timestamp)" "$N8N_USER" "$SAMPLE_INTERVAL_SECONDS" >>"$record"
  say "watching ${N8N_USER}'s sockets into ${record}; Ctrl-C stops it"
  watch
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi

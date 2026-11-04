#!/usr/bin/env bash
# n8n/n8n-cli.sh: one n8n command run by hand, seeing what the daemon sees.
#
# Usage: n8n/n8n-cli.sh <n8n command and flags>, for example: n8n/n8n-cli.sh list:workflow
#
# `sudo -u _n8n n8n ...` alone cannot work: n8n is not on sudo's PATH, `_n8n` cannot enter your
# working directory, and with none of the daemon's environment the command would use another data
# folder with telemetry and the license server back on. So this takes the environment from the
# plist the daemon was started from, and passes nothing else through. Only the log moves, to the
# terminal, so a command's result is seen rather than written to the daemon's file. Run it as
# yourself; sudo asks when it needs to. It holds no value, and n8n prints what it prints.
set -uo pipefail

N8N_USER="_n8n"
N8N_HOME="/var/n8n"
NODE_PATH="/opt/homebrew/bin/node"
N8N_ENTRY="/var/n8n/npm/lib/node_modules/n8n/bin/n8n"
DAEMON_PLIST="/Library/LaunchDaemons/com.zack.hermes-imessage.n8n.plist"
CLI_EXIT_REFUSED=3

cli_refuse() {
  printf 'n8n cli: %s\n' "$*" >&2
  exit "$CLI_EXIT_REFUSED"
}

# as_n8n <command>: runs as `_n8n` from its home, since it cannot enter the directory you are in.
as_n8n() {
  (cd "$N8N_HOME" && sudo -u "$N8N_USER" "$@")
}

# daemon_environment: the plist's variables as NAME=VALUE, each ended by a NUL.
daemon_environment() {
  python3 -c '
import plistlib, sys
with open(sys.argv[1], "rb") as plist_file:
    environment = plistlib.load(plist_file).get("EnvironmentVariables", {})
for name, value in environment.items():
    sys.stdout.write(f"{name}={value}\0")
' "$DAEMON_PLIST"
}

# run_n8n <n8n command and flags>: passes on the command's exit status.
run_n8n() {
  local environment=() variable
  [[ -f "$DAEMON_PLIST" ]] || cli_refuse "${DAEMON_PLIST} is missing; run n8n/install.sh first"
  while IFS= read -r -d '' variable; do
    environment+=("$variable")
  done < <(daemon_environment)
  [[ "${#environment[@]}" -gt 0 ]] || cli_refuse "${DAEMON_PLIST} holds no environment to run n8n in"
  # env takes the last value it is given, so the log's override has to follow the daemon's.
  as_n8n /usr/bin/env -i "${environment[@]}" N8N_LOG_OUTPUT=console "$NODE_PATH" "$N8N_ENTRY" "$@"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  [[ "$#" -gt 0 ]] || cli_refuse "usage: n8n/n8n-cli.sh <n8n command and flags>"
  run_n8n "$@"
fi

#!/usr/bin/env bash
# n8n/install.sh: n8n as the `_n8n` role account under a LaunchDaemon, on 127.0.0.1 alone,
# with every switch that sends something off the Mac turned off (the design's "Install,
# service and hardening"). Run by hand with sudo, on the Mac Studio alone (n8n/README.md).
# In order, stopping at the first refusal:
#   1. refuses unless it runs as root, on the Mac Studio, with Homebrew's node where the
#      plist starts it and new enough for n8n;
#   2. makes the `_n8n` group and user, GID and UID 450, with no password and its home at
#      /var/n8n, or refuses one that exists otherwise, or the ID held by another name; then
#      refuses if `_n8n` can list the home of whoever ran this;
#   3. makes /var/n8n, its log directory and its data directory, and sets the owner, group
#      and mode of those three directories alone, never of anything in them: /var/n8n/.n8n
#      holds the encryption key and the database. Refuses one that someone else owns;
#   4. installs the pinned n8n as `_n8n`, unless that version is already installed;
#   5. makes the three log files group admin, mode 640, so Zack can tail them without sudo;
#   6. copies the repository's plist to /Library/LaunchDaemons when the installed copy differs;
#   7. refuses while another user listens on n8n's port or its task broker's;
#   8. starts the daemon. The first start waits for the egress watch (n8n/watch-egress.sh),
#      since that start is when n8n would first call home. A changed plist is booted out and
#      in again, since launchd keeps the definition it loaded; a new n8n version is restarted;
#   9. waits for n8n's health check, then stops the daemon again if anything of `_n8n`
#      listens on an address other than 127.0.0.1.
# A run with nothing to change says so and exits 0. It never deletes the user, never touches
# what /var/n8n/.n8n holds and never removes anything: uninstalling is in n8n/README.md.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The one machine this is for: `hostname -s`, as the dotfiles' setup.sh names it.
MAC_STUDIO_HOST_NAME="ZacksMacStudio"
# The newest 2.45.x on build day. Upgrading is a deliberate step: change it, run this again.
N8N_VERSION="2.45.7"
# n8n 2.45's package.json asks for `engines.node >=24.0.0`.
NODE_MAJOR_VERSION_MINIMUM=24
NODE_PATH="/opt/homebrew/bin/node"
NPM_PATH="/opt/homebrew/bin/npm"

N8N_USER="_n8n"
N8N_GROUP="_n8n"
# macOS keeps 450 to 499 for role accounts, whose names start with an underscore.
N8N_UID=450
N8N_GID=450
ADMIN_GROUP="admin"
ROOT_USER="root"
WHEEL_GROUP="wheel"

N8N_HOME="/var/n8n"
N8N_LOG_DIRECTORY="${N8N_HOME}/log"
# n8n appends `.n8n` to N8N_USER_FOLDER itself.
N8N_DATA_DIRECTORY="${N8N_HOME}/.n8n"
N8N_NPM_PREFIX="${N8N_HOME}/npm"
N8N_NPM_CACHE="${N8N_HOME}/.npm"
N8N_PACKAGE_JSON="${N8N_NPM_PREFIX}/lib/node_modules/n8n/package.json"
# The account's record, attribute by attribute, as made and as read back on every run. A
# Password of `*` is no password at all: nothing can log in as `_n8n`. No value may hold a
# space: dscl prints such a value on a line of its own, which directory_record cannot read.
USER_ATTRIBUTE_NAMES=(UniqueID PrimaryGroupID UserShell NFSHomeDirectory Password RealName)
USER_ATTRIBUTE_VALUES=("$N8N_UID" "$N8N_GID" /usr/bin/false "$N8N_HOME" '*' n8n)
LOG_FILES=("${N8N_LOG_DIRECTORY}/n8n.log" "${N8N_LOG_DIRECTORY}/launchd.out.log" "${N8N_LOG_DIRECTORY}/launchd.err.log")
HOME_MODE=750
LOG_DIRECTORY_MODE=750
DATA_DIRECTORY_MODE=700
LOG_FILE_MODE=640

DAEMON_LABEL="com.zack.hermes-imessage.n8n"
DAEMON_SERVICE="system/${DAEMON_LABEL}"
DAEMON_TEMPLATE="${SCRIPT_DIR}/launchd/${DAEMON_LABEL}.plist"
DAEMON_PATH="/Library/LaunchDaemons/${DAEMON_LABEL}.plist"
DAEMON_PLIST_MODE=644
# Written by watch-egress.sh while it runs; the same path is named there.
EGRESS_WATCH_NAME="watch-egress.sh"
EGRESS_WATCH_PIDFILE="/var/run/${DAEMON_LABEL}.egress-watch.pid"

LISTEN_ADDRESS="127.0.0.1"
N8N_PORT=5678
TASK_BROKER_PORT=5679
HEALTH_URL="http://${LISTEN_ADDRESS}:${N8N_PORT}/healthz"
HEALTH_OK_STATUS=200
HEALTH_POLL_SECONDS=2
# A first start runs n8n's database migrations, which take a while on an empty database.
HEALTH_WAIT_SECONDS=180
BOOTOUT_WAIT_SECONDS=15

INSTALL_EXIT_REFUSED=3
INSTALL_EXIT_WAITING_FOR_WATCH=4
INSTALL_EXIT_UNHEALTHY=5

# What this run changed, one line each; none at the end means there was nothing to do.
changes=()
plist_replaced=false
n8n_upgraded=false

say() {
  printf 'n8n install: %s\n' "$*"
}

refuse() {
  printf 'n8n install: %s\n' "$*" >&2
  exit "$INSTALL_EXIT_REFUSED"
}

changed() {
  changes+=("$*")
  say "$*"
}

# directory_record <record> <attribute>: one attribute of a Directory Services record,
# empty when the record or the attribute is absent.
directory_record() {
  dscl . -read "$1" "$2" 2>/dev/null | awk -v attribute="${2}:" '$1 == attribute { print $2 }'
}

# holder_of <records> <attribute> <value>: the name of the record that holds the value, if any.
holder_of() {
  dscl . -list "$1" "$2" | awk -v value="$3" '$2 == value { print $1 }'
}

check_machine() {
  local host_name node_major
  [[ "$EUID" -eq 0 ]] || refuse "run this with sudo: it makes a user and a LaunchDaemon"
  [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "$ROOT_USER" ]] \
    || refuse "run this with sudo from your own account: it checks that ${N8N_USER} cannot read your home"
  host_name="$(hostname -s)"
  [[ "$host_name" == "$MAC_STUDIO_HOST_NAME" ]] \
    || refuse "this is for the Mac Studio (${MAC_STUDIO_HOST_NAME}) alone; this machine is ${host_name}"
  [[ -x "$NODE_PATH" && -x "$NPM_PATH" ]] \
    || refuse "Homebrew's node is not at ${NODE_PATH}, where the plist starts it; brew install node, then run this again"
  node_major="$("$NODE_PATH" -p 'process.versions.node.split(".")[0]')" \
    || refuse "${NODE_PATH} did not report its version"
  (( node_major >= NODE_MAJOR_VERSION_MINIMUM )) \
    || refuse "n8n ${N8N_VERSION} needs Node ${NODE_MAJOR_VERSION_MINIMUM} or later; ${NODE_PATH} is ${node_major}"
}

ensure_group() {
  local existing_gid holder
  existing_gid="$(directory_record "/Groups/${N8N_GROUP}" PrimaryGroupID)"
  if [[ -n "$existing_gid" ]]; then
    [[ "$existing_gid" == "$N8N_GID" ]] || refuse "group ${N8N_GROUP} exists with GID ${existing_gid}, not ${N8N_GID}"
    return 0
  fi
  holder="$(holder_of /Groups PrimaryGroupID "$N8N_GID")"
  [[ -z "$holder" ]] || refuse "GID ${N8N_GID} belongs to the group ${holder}"
  dseditgroup -o create -i "$N8N_GID" "$N8N_GROUP" || refuse "dseditgroup could not make ${N8N_GROUP}"
  changed "made the group ${N8N_GROUP} (GID ${N8N_GID})"
}

# check_user_record: every attribute the account is made with, read back; refuses the first
# that differs, naming how to start again while nothing depends on the record.
check_user_record() {
  local attribute expected actual
  for attribute in "${!USER_ATTRIBUTE_NAMES[@]}"; do
    expected="${USER_ATTRIBUTE_VALUES[$attribute]}"
    actual="$(directory_record "/Users/${N8N_USER}" "${USER_ATTRIBUTE_NAMES[$attribute]}")"
    [[ "$actual" == "$expected" ]] || refuse "user ${N8N_USER} has ${USER_ATTRIBUTE_NAMES[$attribute]}" \
      "'${actual}', not '${expected}'. Before n8n's first start, 'sudo dscl . -delete /Users/${N8N_USER}'" \
      "removes the record alone, and this script makes it again"
  done
}

# The account is made with dscl, the way macOS makes its own daemon accounts: sysadminctl's
# -roleAccount ignores -home (it set /var/empty), sets a password, and makes an account
# opendirectoryd counts in staff, which can enter Zack's home (measured 2026-11-03).
ensure_user() {
  local holder attribute
  if [[ -n "$(directory_record "/Users/${N8N_USER}" UniqueID)" ]]; then
    check_user_record
    return 0
  fi
  holder="$(holder_of /Users UniqueID "$N8N_UID")"
  [[ -z "$holder" ]] || refuse "UID ${N8N_UID} belongs to the user ${holder}"
  dscl . -create "/Users/${N8N_USER}" || refuse "dscl could not make ${N8N_USER}"
  for attribute in "${!USER_ATTRIBUTE_NAMES[@]}"; do
    dscl . -create "/Users/${N8N_USER}" "${USER_ATTRIBUTE_NAMES[$attribute]}" "${USER_ATTRIBUTE_VALUES[$attribute]}" \
      || refuse "dscl could not set ${USER_ATTRIBUTE_NAMES[$attribute]} on ${N8N_USER}"
  done
  # A record made under a name just deleted can be answered from the old one's cached groups.
  dsmemberutil flushcache
  check_user_record
  changed "made the role account ${N8N_USER} (UID ${N8N_UID}), home ${N8N_HOME}, no password"
}

# check_isolation: fails closed before n8n runs as a user that can enter Zack's home. The probe
# is the gate itself, not one cause of it, so it also catches a widened home or a new group.
check_isolation() {
  local home
  home="$(directory_record "/Users/${SUDO_USER}" NFSHomeDirectory)"
  [[ -d "$home" ]] || refuse "cannot find the home of ${SUDO_USER}"
  if sudo -u "$N8N_USER" /bin/ls "$home" >/dev/null 2>&1; then
    refuse "${N8N_USER} can list ${home}, so n8n could read your files; its groups are $(id -Gn "$N8N_USER")"
  fi
}

# ensure_directory <path> <owner> <group> <mode>: the directory itself, never its contents.
# A directory owned by someone else is refused; an empty one owned by root is taken over,
# since that is what sysadminctl may leave for a home it was told about.
ensure_directory() {
  local path="$1" owner="$2" group="$3" mode="$4" current_owner
  if [[ ! -e "$path" ]]; then
    install -d -o "$owner" -g "$group" -m "$mode" "$path" || refuse "could not make ${path}"
    changed "made ${path} (${owner}:${group}, ${mode})"
    return 0
  fi
  [[ -d "$path" && ! -L "$path" ]] || refuse "${path} exists and is not a directory"
  current_owner="$(stat -f %Su "$path")"
  if [[ "$current_owner" == "$ROOT_USER" && "$owner" != "$ROOT_USER" ]]; then
    [[ -z "$(ls -A "$path")" ]] || refuse "${path} is owned by root and not empty; it is not this install's"
    chown "$owner" "$path" || refuse "could not give ${path} to ${owner}"
    changed "took over the empty ${path} for ${owner}"
  elif [[ "$current_owner" != "$owner" ]]; then
    refuse "${path} is owned by ${current_owner}, not ${owner}; it is not this install's"
  fi
  if [[ "$(stat -f %Sg "$path")" != "$group" ]]; then
    chgrp "$group" "$path" || refuse "could not set the group of ${path}"
    changed "set the group of ${path} to ${group}"
  fi
  if [[ "$(stat -f %Lp "$path")" != "$mode" ]]; then
    chmod "$mode" "$path" || refuse "could not set the mode of ${path}"
    changed "set the mode of ${path} to ${mode}"
  fi
}

installed_n8n_version() {
  [[ -f "$N8N_PACKAGE_JSON" ]] || return 0
  "$NODE_PATH" -p 'require(process.argv[1]).version' "$N8N_PACKAGE_JSON"
}

ensure_n8n() {
  local installed
  installed="$(installed_n8n_version)" || refuse "could not read the installed n8n's version from ${N8N_PACKAGE_JSON}"
  [[ "$installed" != "$N8N_VERSION" ]] || return 0
  say "installing n8n ${N8N_VERSION} as ${N8N_USER}: _n8n's one outbound connection, to the npm registry"
  # Install scripts stay allowed: sqlite fetches its prebuilt binary. The working directory
  # is _n8n's own home, since npm cannot start in a directory _n8n cannot read.
  (cd "$N8N_HOME" && sudo -u "$N8N_USER" env HOME="$N8N_HOME" PATH="$(dirname "$NODE_PATH"):/usr/bin:/bin" \
    npm_config_cache="$N8N_NPM_CACHE" "$NPM_PATH" install -g --prefix "$N8N_NPM_PREFIX" "n8n@${N8N_VERSION}") \
    || refuse "npm could not install n8n ${N8N_VERSION}"
  [[ "$(installed_n8n_version)" == "$N8N_VERSION" ]] || refuse "npm finished but ${N8N_PACKAGE_JSON} is not ${N8N_VERSION}"
  changed "installed n8n ${N8N_VERSION}${installed:+ over ${installed}}"
  n8n_upgraded=true
}

ensure_log_files() {
  local log_file
  for log_file in "${LOG_FILES[@]}"; do
    if [[ ! -e "$log_file" ]]; then
      install -o "$N8N_USER" -g "$ADMIN_GROUP" -m "$LOG_FILE_MODE" /dev/null "$log_file" || refuse "could not make ${log_file}"
      changed "made ${log_file} (${N8N_USER}:${ADMIN_GROUP}, ${LOG_FILE_MODE})"
      continue
    fi
    if [[ "$(stat -f %Sg "$log_file")" != "$ADMIN_GROUP" ]]; then
      chgrp "$ADMIN_GROUP" "$log_file" || refuse "could not set the group of ${log_file}"
      changed "set the group of ${log_file} to ${ADMIN_GROUP}"
    fi
    if [[ "$(stat -f %Lp "$log_file")" != "$LOG_FILE_MODE" ]]; then
      chmod "$LOG_FILE_MODE" "$log_file" || refuse "could not set the mode of ${log_file}"
      changed "set the mode of ${log_file} to ${LOG_FILE_MODE}"
    fi
  done
}

ensure_daemon_plist() {
  plutil -lint "$DAEMON_TEMPLATE" >/dev/null || refuse "${DAEMON_TEMPLATE} is not a valid plist"
  if [[ -f "$DAEMON_PATH" ]] && cmp -s "$DAEMON_TEMPLATE" "$DAEMON_PATH"; then
    return 0
  fi
  install -o "$ROOT_USER" -g "$WHEEL_GROUP" -m "$DAEMON_PLIST_MODE" "$DAEMON_TEMPLATE" "$DAEMON_PATH" \
    || refuse "could not write ${DAEMON_PATH}"
  changed "wrote ${DAEMON_PATH}"
  plist_replaced=true
}

daemon_loaded() {
  launchctl print "$DAEMON_SERVICE" >/dev/null 2>&1
}

# listener_uids <port>: the UID of every process listening on the TCP port, one a line.
listener_uids() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN -Fu 2>/dev/null | sed -n 's/^u//p' | sort -u
}

check_ports() {
  local port uid
  for port in "$N8N_PORT" "$TASK_BROKER_PORT"; do
    for uid in $(listener_uids "$port"); do
      [[ "$uid" == "$N8N_UID" ]] || refuse "UID ${uid} already listens on port ${port}; n8n needs it"
    done
  done
}

egress_watch_running() {
  local watch_pid
  [[ -f "$EGRESS_WATCH_PIDFILE" ]] || return 1
  watch_pid="$(cat "$EGRESS_WATCH_PIDFILE")"
  [[ "$(ps -o command= -p "$watch_pid" 2>/dev/null)" == *"$EGRESS_WATCH_NAME"* ]]
}

wait_for_bootout() {
  local waited=0
  while daemon_loaded; do
    (( waited < BOOTOUT_WAIT_SECONDS )) || refuse "launchd still holds ${DAEMON_LABEL} ${BOOTOUT_WAIT_SECONDS}s after bootout"
    sleep 1
    waited=$((waited + 1))
  done
}

start_daemon() {
  if ! daemon_loaded; then
    if ! egress_watch_running; then
      printf 'n8n install: %s\n' \
        "everything is in place, but n8n has not started. Its first start is when it would" \
        "call home, so start the egress watch in another terminal first:" \
        "  sudo ${SCRIPT_DIR}/${EGRESS_WATCH_NAME}" \
        "then run this again." >&2
      exit "$INSTALL_EXIT_WAITING_FOR_WATCH"
    fi
    launchctl bootstrap system "$DAEMON_PATH" || refuse "launchctl could not bootstrap ${DAEMON_PATH}"
    changed "started ${DAEMON_LABEL} at $(date '+%Y-%m-%dT%H:%M:%S%z')"
  elif [[ "$plist_replaced" == true ]]; then
    launchctl bootout "$DAEMON_SERVICE" || refuse "launchctl could not boot out ${DAEMON_LABEL}"
    wait_for_bootout
    launchctl bootstrap system "$DAEMON_PATH" || refuse "launchctl could not bootstrap ${DAEMON_PATH}"
    changed "reloaded ${DAEMON_LABEL} with the new plist"
  elif [[ "$n8n_upgraded" == true ]]; then
    launchctl kickstart -k "$DAEMON_SERVICE" || refuse "launchctl could not restart ${DAEMON_LABEL}"
    changed "restarted ${DAEMON_LABEL} on n8n ${N8N_VERSION}"
  fi
}

wait_for_health() {
  local waited=0 status
  while (( waited < HEALTH_WAIT_SECONDS )); do
    status="$(curl -s -o /dev/null -w '%{http_code}' --max-time "$HEALTH_POLL_SECONDS" "$HEALTH_URL")"
    [[ "$status" == "$HEALTH_OK_STATUS" ]] && return 0
    sleep "$HEALTH_POLL_SECONDS"
    waited=$((waited + HEALTH_POLL_SECONDS))
  done
  printf 'n8n install: %s did not answer %s within %ss; read %s/launchd.err.log and n8n.log\n' \
    "$HEALTH_URL" "$HEALTH_OK_STATUS" "$HEALTH_WAIT_SECONDS" "$N8N_LOG_DIRECTORY" >&2
  exit "$INSTALL_EXIT_UNHEALTHY"
}

# Fails closed: n8n listening anywhere but the loopback address puts the relay on the LAN.
check_listen_addresses() {
  local listeners outside
  listeners="$(lsof -nP -iTCP -sTCP:LISTEN -a -u "$N8N_USER" -Fn | sed -n 's/^n//p')"
  [[ -n "$listeners" ]] || refuse "n8n answers its health check, but ${N8N_USER} holds no listening socket"
  outside="$(printf '%s\n' "$listeners" | grep -v "^${LISTEN_ADDRESS//./\\.}:")"
  [[ -z "$outside" ]] && return 0
  launchctl bootout "$DAEMON_SERVICE"
  refuse "stopped n8n: ${N8N_USER} listened beyond ${LISTEN_ADDRESS}, on ${outside//$'\n'/, }"
}

main() {
  check_machine
  ensure_group
  ensure_user
  check_isolation
  ensure_directory "$N8N_HOME" "$N8N_USER" "$ADMIN_GROUP" "$HOME_MODE"
  ensure_directory "$N8N_LOG_DIRECTORY" "$N8N_USER" "$ADMIN_GROUP" "$LOG_DIRECTORY_MODE"
  ensure_directory "$N8N_DATA_DIRECTORY" "$N8N_USER" "$N8N_GROUP" "$DATA_DIRECTORY_MODE"
  ensure_n8n
  ensure_log_files
  ensure_daemon_plist
  check_ports
  start_daemon
  wait_for_health
  check_listen_addresses
  if [[ "${#changes[@]}" -eq 0 ]]; then
    say "nothing to do; n8n ${N8N_VERSION} answers at ${HEALTH_URL} and listens on ${LISTEN_ADDRESS} alone"
  else
    say "done, ${#changes[@]} changes; n8n ${N8N_VERSION} answers at ${HEALTH_URL} and listens on ${LISTEN_ADDRESS} alone"
  fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi

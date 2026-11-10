#!/usr/bin/env bash
# n8n/import.sh: the relay template, rendered with the handle file's values, into n8n.
#
# Usage: n8n/import.sh RUN_RECORD_TABLE_ID
#
# Run it as yourself: the handle file is yours, and `_n8n` cannot read it. It renders the template
# into a folder only you can read, hands that to `_n8n` through a pipe into a folder of `_n8n`'s,
# imports it, and removes both copies whether or not the import worked, since they hold handles.
# It refuses while a workflow of the relay's name exists: two copies would hold one webhook path
# between them, and only one could be published. An imported workflow always arrives unpublished.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=n8n/n8n-cli.sh
source "${SCRIPT_DIR}/n8n-cli.sh" || exit 1

HANDLE_FILE="${HOME}/.hermes/profiles/imessage/data/imessage/zack-handles.txt"
TEMPLATE="${SCRIPT_DIR}/workflows/imessage-relay.json"
RENDER_SCRIPT="${SCRIPT_DIR}/render.py"
WORKFLOW_NAME="iMessage relay"
RENDERED_FILE_NAME="relay.json"
IMPORT_EXIT_REFUSED=3

rendered_directory=""
handed_over_directory=""

import_refuse() {
  printf 'n8n import: %s\n' "$*" >&2
  exit "$IMPORT_EXIT_REFUSED"
}

remove_rendered_copies() {
  if [[ -n "$rendered_directory" ]]; then
    rm -rf "$rendered_directory" \
      || printf 'n8n import: could not remove %s, which holds handles; remove it yourself\n' "$rendered_directory" >&2
  fi
  if [[ -n "$handed_over_directory" ]]; then
    as_n8n /bin/rm -rf "$handed_over_directory" \
      || printf 'n8n import: could not remove %s, which holds handles; sudo rm -rf it\n' "$handed_over_directory" >&2
  fi
}

# relay_ids <list>: the id of each workflow of the relay's name, from `list:workflow`'s id|name lines.
relay_ids() {
  printf '%s\n' "$1" | awk -F'|' -v name="$WORKFLOW_NAME" '$2 == name { print $1 }'
}

listed_workflows() {
  local workflows
  workflows="$(run_n8n list:workflow)" || import_refuse "n8n could not list its workflows"
  printf '%s\n' "$workflows"
}

main() {
  local table_id="${1:-}" existing_relays handed_over_file new_relay
  [[ -n "$table_id" ]] || import_refuse "usage: n8n/import.sh RUN_RECORD_TABLE_ID"
  [[ "$EUID" -ne 0 ]] || import_refuse "run this as yourself, not with sudo: the handle file is yours, and it asks for sudo when it needs it"
  trap remove_rendered_copies EXIT

  existing_relays="$(listed_workflows)" || exit "$?"
  existing_relays="$(relay_ids "$existing_relays")"
  [[ -z "$existing_relays" ]] \
    || import_refuse "a workflow named '${WORKFLOW_NAME}' already exists (${existing_relays//$'\n'/, }); delete it in the editor first"

  rendered_directory="$(mktemp -d)" || import_refuse "could not make a private folder to render into"
  python3 "$RENDER_SCRIPT" "$HANDLE_FILE" "$TEMPLATE" "${rendered_directory}/${RENDERED_FILE_NAME}" \
    --run-record-table-id "$table_id" || import_refuse "render.py refused, so nothing was imported"

  # Written and later removed by `_n8n`, never root: `_n8n` owns this folder, so a path in it that
  # root acted on could have been pointed elsewhere.
  handed_over_directory="$(as_n8n /usr/bin/mktemp -d "${N8N_HOME}/import.XXXXXX")" \
    || import_refuse "could not make a folder ${N8N_USER} can read"
  handed_over_file="${handed_over_directory}/${RENDERED_FILE_NAME}"
  # shellcheck disable=SC2016  # the $1 belongs to the inner shell.
  as_n8n /bin/sh -c 'umask 077; cat >"$1"' sh "$handed_over_file" <"${rendered_directory}/${RENDERED_FILE_NAME}" \
    || import_refuse "could not hand the rendered workflow to ${N8N_USER}"

  run_n8n import:workflow "--input=${handed_over_file}" || import_refuse "n8n could not import the workflow"

  new_relay="$(listed_workflows)" || exit "$?"
  new_relay="$(relay_ids "$new_relay")"
  [[ -n "$new_relay" ]] || import_refuse "n8n said it imported, but lists no workflow named '${WORKFLOW_NAME}'"
  printf 'n8n import: imported as %s. Publish it in the editor, http://127.0.0.1:5678; its webhook answers nothing until then.\n' "$new_relay"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi

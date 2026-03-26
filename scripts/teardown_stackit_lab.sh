#!/usr/bin/env bash
set -euo pipefail

# Preferred teardown helper for this repository.
#
# Notes:
# - Uses the local repo checkout to find Terraform state and tfvars.
# - Retries `terraform destroy` and performs targeted vManage volume-detach
#   cleanup when the provider gets stuck on data disks.
# - Run this helper from the checkout you want to destroy.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF="${TF_BIN:-terraform}"
STACKIT="${STACKIT_BIN:-stackit}"
DESTROY_ARGS=("$@")
DEFAULT_STACKIT_SERVICE_ACCOUNT_KEY_PATH="${HOME}/.stackit/terraform-sa-key.json"

if ! command -v "$TF" >/dev/null 2>&1; then
  echo "terraform not found in PATH" >&2
  exit 127
fi

if ! command -v "$STACKIT" >/dev/null 2>&1; then
  echo "stackit CLI not found in PATH" >&2
  exit 127
fi

if [[ ! -f "$REPO_DIR/terraform.tfvars" ]]; then
  echo "Missing $REPO_DIR/terraform.tfvars" >&2
  exit 1
fi

if [[ -z "${STACKIT_SERVICE_ACCOUNT_KEY_PATH:-}" && -f "$DEFAULT_STACKIT_SERVICE_ACCOUNT_KEY_PATH" ]]; then
  export STACKIT_SERVICE_ACCOUNT_KEY_PATH="$DEFAULT_STACKIT_SERVICE_ACCOUNT_KEY_PATH"
fi

parse_tfvars_string() {
  local key="$1"
  python3 - "$REPO_DIR/terraform.tfvars" "$key" <<'PY'
import re, sys
text = open(sys.argv[1], encoding='utf-8').read()
key = re.escape(sys.argv[2])
match = re.search(rf'(?m)^\s*{key}\s*=\s*"([^"]*)"\s*$', text)
if not match:
    sys.exit(1)
print(match.group(1), end="")
PY
}

PROJECT_ID="$(parse_tfvars_string project_id)"
REGION="$(parse_tfvars_string region)"

log() {
  printf '[teardown] %s\n' "$*"
}

sleep_with_log() {
  local seconds="$1"
  shift
  log "sleeping ${seconds}s: $*"
  sleep "$seconds"
}

activate_stackit_cli_auth() {
  if [[ -n "${STACKIT_SERVICE_ACCOUNT_KEY_PATH:-}" && -f "${STACKIT_SERVICE_ACCOUNT_KEY_PATH}" ]]; then
    export STACKIT_CLI_CONFIG_DIR
    STACKIT_CLI_CONFIG_DIR="${STACKIT_CLI_CONFIG_DIR:-$(mktemp -d)}"
    "$STACKIT" auth activate-service-account --service-account-key-path "$STACKIT_SERVICE_ACCOUNT_KEY_PATH" -y >/dev/null 2>&1 || true
  fi
}

activate_stackit_cli_auth

tf_destroy() {
  if ((${#DESTROY_ARGS[@]} > 0)); then
    "$TF" -chdir="$REPO_DIR" destroy -auto-approve "${DESTROY_ARGS[@]}"
  else
    "$TF" -chdir="$REPO_DIR" destroy -auto-approve
  fi
}

get_controller_inventory() {
  "$TF" -chdir="$REPO_DIR" output -json controller_inventory 2>/dev/null || echo '{}'
}

get_vmanage_attach_keys() {
  "$TF" -chdir="$REPO_DIR" state list 2>/dev/null | grep 'stackit_server_volume_attach.vmanage_data' | sed -E 's/.*\["([^"]+)"\].*/\1/' || true
}

get_vmanage_keys() {
  "$TF" -chdir="$REPO_DIR" state list 2>/dev/null | grep 'stackit_volume.vmanage_data' | sed -E 's/.*\["([^"]+)"\].*/\1/' || true
}

volume_id_for_key() {
  local key="$1"
  "$TF" -chdir="$REPO_DIR" state show "stackit_server_volume_attach.vmanage_data[\"$key\"]" 2>/dev/null | awk -F' = ' '/^volume_id = /{print $2; exit}' || true
}

remove_attach_state_for_key() {
  local key="$1"
  "$TF" -chdir="$REPO_DIR" state rm "stackit_server_volume_attach.vmanage_data[\"$key\"]" >/dev/null 2>&1 || true
}

server_is_stopped() {
  local server_id="$1"
  local status power
  read -r status power < <(
    "$STACKIT" server describe "$server_id" --project-id "$PROJECT_ID" --region "$REGION" -o json 2>/dev/null \
      | python3 -c 'import json,sys
try:
    payload=json.load(sys.stdin)
except Exception:
    print("", "")
    raise SystemExit(0)
print(str(payload.get("status","")), str(payload.get("powerStatus","")))' || true
  )
  [[ "$status" == "INACTIVE" || "$power" == "STOPPED" ]]
}

wait_for_server_stop() {
  local server_id="$1"
  local timeout_seconds="${2:-600}"
  local deadline=$((SECONDS + timeout_seconds))
  while (( SECONDS < deadline )); do
    if server_is_stopped "$server_id"; then
      return 0
    fi
    sleep_with_log 10 "waiting for server $server_id to stop before detaching the vManage data volume"
  done
  return 1
}

volume_attached_to_server() {
  local server_id="$1"
  local volume_id="$2"
  "$STACKIT" server describe "$server_id" --project-id "$PROJECT_ID" --region "$REGION" -o json 2>/dev/null \
    | python3 -c 'import json,sys
volume_id=sys.argv[1]
try:
    payload=json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
volumes=payload.get("volumes") or []
raise SystemExit(0 if volume_id in volumes else 1)' "$volume_id"
}

detach_volume_with_retries() {
  local key="$1"
  local server_id="$2"
  local volume_id="$3"
  local attempt output rc
  for attempt in 1 2 3 4 5; do
    log "Detaching $key data volume $volume_id from $server_id (attempt $attempt/5)"
    set +e
    output="$("$STACKIT" server volume detach "$volume_id" --server-id "$server_id" --project-id "$PROJECT_ID" --region "$REGION" -y 2>&1)"
    rc=$?
    set -e
    if (( rc == 0 )); then
      log "$output"
    else
      log "detach command returned exit $rc for $key: $output"
    fi
    if ! volume_attached_to_server "$server_id" "$volume_id"; then
      log "$key data volume is no longer attached to $server_id"
      remove_attach_state_for_key "$key"
      return 0
    fi
    sleep_with_log 10 "retrying detach validation for $key data volume"
  done
  return 1
}

detach_vmanage_data_volumes() {
  local inventory_json="$1"
  local key server_id volume_id

  while IFS= read -r key; do
    [[ -n "$key" ]] || continue
    server_id="$(python3 - "$inventory_json" "$key" <<'PY'
import json, sys
data = json.loads(sys.argv[1] or "{}")
key = sys.argv[2]
print(data.get(key, {}).get("server_id", ""), end="")
PY
)"
    volume_id="$(volume_id_for_key "$key")"

    if [[ -z "$server_id" || -z "$volume_id" ]]; then
      log "Skipping $key because server_id or volume_id is missing"
      continue
    fi

    log "Stopping $key server $server_id"
    "$STACKIT" server stop "$server_id" --project-id "$PROJECT_ID" --region "$REGION" --async -y >/dev/null 2>&1 || true
    if ! wait_for_server_stop "$server_id" 900; then
      log "Server $server_id did not report STOPPED before timeout; attempting detach anyway"
    fi

    if ! detach_volume_with_retries "$key" "$server_id" "$volume_id"; then
      log "Failed to detach $key data volume $volume_id from $server_id after retries"
    fi
  done < <(get_vmanage_attach_keys)
}

log "Running terraform destroy"
if tf_destroy; then
  log "terraform destroy completed successfully"
  exit 0
fi

log "terraform destroy failed; attempting vManage data-volume recovery"
INVENTORY_JSON="$(get_controller_inventory)"
detach_vmanage_data_volumes "$INVENTORY_JSON"

log "Waiting 20 seconds for detach operations to settle"
sleep_with_log 20 "allowing STACKIT detach operations to settle before retrying terraform destroy"

log "Retrying terraform destroy"
if tf_destroy; then
  log "terraform destroy completed successfully after recovery"
  exit 0
fi

log "terraform destroy still failed after recovery"
exit 1

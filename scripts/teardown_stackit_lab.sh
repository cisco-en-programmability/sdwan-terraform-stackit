#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF="${TF_BIN:-terraform}"
STACKIT="${STACKIT_BIN:-stackit}"
DESTROY_ARGS=("$@")

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

get_vmanage_keys() {
  "$TF" -chdir="$REPO_DIR" state list 2>/dev/null | grep 'stackit_volume.vmanage_data' | sed -E 's/.*\["([^"]+)"\].*/\1/' || true
}

volume_id_for_key() {
  local key="$1"
  "$TF" -chdir="$REPO_DIR" state show "stackit_volume.vmanage_data[\"$key\"]" 2>/dev/null | awk -F' = ' '/^id = /{print $2; exit}' || true
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
    "$STACKIT" server stop "$server_id" --project-id "$PROJECT_ID" --region "$REGION" -y >/dev/null 2>&1 || true

    log "Detaching $key data volume $volume_id from $server_id"
    "$STACKIT" server volume detach "$volume_id" --server-id "$server_id" --project-id "$PROJECT_ID" --region "$REGION" -y >/dev/null 2>&1 || true
  done < <(get_vmanage_keys)
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
sleep 20

log "Retrying terraform destroy"
if tf_destroy; then
  log "terraform destroy completed successfully after recovery"
  exit 0
fi

log "terraform destroy still failed after recovery"
exit 1

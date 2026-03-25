#!/usr/bin/env bash
set -euo pipefail

# Handle the interactive vManage `/dev/vdb` first-boot prompt on a single node.
#
# Notes:
# - `run_vmanage_firstboot_init` is intentionally false by default in Terraform.
# - The normal operator workflow is to verify `terraform apply` first, then run
#   this helper explicitly or through `stackit_disk_format.py`.
# - Success is based on the data-mount outcome, not on early HTTPS alone.

host="${1:?usage: init_vmanage_firstboot.sh <host> <password> [user] [preferred_devices_csv] [https_host]}"
password="${2:?usage: init_vmanage_firstboot.sh <host> <password> [user] [preferred_devices_csv] [https_host]}"
user="${3:-admin}"
preferred_devices="${4:-vdb,sdb,xvdb,nvme1n1,hdc,sdc}"
https_host="${5:-$host}"

ssh_timeout_seconds="${VMANAGE_INIT_SSH_TIMEOUT_SECONDS:-900}"
reboot_timeout_seconds="${VMANAGE_INIT_REBOOT_TIMEOUT_SECONDS:-1200}"
https_timeout_seconds="${VMANAGE_INIT_HTTPS_TIMEOUT_SECONDS:-3600}"
data_mount_timeout_seconds="${VMANAGE_INIT_DATA_MOUNT_TIMEOUT_SECONDS:-5400}"
wait_for_https_after_init="${VMANAGE_INIT_WAIT_FOR_HTTPS:-0}"

log() {
  printf '[%s] %s\n' "$host" "$*"
}

pause_with_log() {
  local seconds="$1"
  shift
  log "sleeping ${seconds}s: $*"
  sleep "$seconds"
}

wait_for_port() {
  local target_host="$1"
  local target_port="$2"
  local timeout_seconds="$3"
  local deadline=$((SECONDS + timeout_seconds))
  local last_notice=0

  while (( SECONDS < deadline )); do
    if nc -z -w 2 "$target_host" "$target_port" >/dev/null 2>&1; then
      return 0
    fi
    if (( SECONDS - last_notice >= 60 )); then
      log "waiting for ${target_host}:${target_port} to open"
      last_notice=$SECONDS
    fi
    pause_with_log 5 "retrying ${target_host}:${target_port} open check"
  done

  return 1
}

wait_for_port_down() {
  local target_host="$1"
  local target_port="$2"
  local timeout_seconds="$3"
  local deadline=$((SECONDS + timeout_seconds))
  local last_notice=0

  while (( SECONDS < deadline )); do
    if ! nc -z -w 2 "$target_host" "$target_port" >/dev/null 2>&1; then
      return 0
    fi
    if (( SECONDS - last_notice >= 60 )); then
      log "waiting for ${target_host}:${target_port} to close"
      last_notice=$SECONDS
    fi
    pause_with_log 5 "retrying ${target_host}:${target_port} closed check"
  done

  return 1
}

wait_for_https() {
  local target_host="$1"
  local timeout_seconds="$2"
  local deadline=$((SECONDS + timeout_seconds))
  local last_notice=0

  while (( SECONDS < deadline )); do
    if curl --silent --show-error --insecure --connect-timeout 5 --max-time 10 "https://${target_host}:8443" >/dev/null 2>&1; then
      return 0
    fi
    if curl --silent --show-error --insecure --connect-timeout 5 --max-time 10 "https://${target_host}" >/dev/null 2>&1; then
      return 0
    fi
    if (( SECONDS - last_notice >= 60 )); then
      log "waiting for HTTPS on ${target_host}"
      last_notice=$SECONDS
    fi
    pause_with_log 10 "retrying HTTPS check for ${target_host}"
  done

  return 1
}

check_data_mount() {
  local target_host="$1"
  local output=""
  local shell_rc=0
  local source_line=""

  set +e
  output="$(
    printf '%s\n%s\n' \
      "vshell" \
      "mountpoint -q /opt/data && findmnt -n -o SOURCE /opt/data | sed 's/^/__OPT_DATA_SOURCE__ /' || echo __OPT_DATA_MISSING__" \
      | sshpass -p "$password" ssh \
          -o StrictHostKeyChecking=no \
          -o UserKnownHostsFile=/dev/null \
          -o ConnectTimeout=10 \
          -o LogLevel=ERROR \
          "${user}@${target_host}" 2>&1
  )"
  shell_rc=$?
  set -e

  if grep -Eiq 'select storage device to use:|would you like to format .*\(y/n\):' <<<"$output"; then
    echo "interactive storage prompt still present on $target_host" >&2
    return 20
  fi

  if (( shell_rc != 0 )); then
    if [[ -n "$output" ]]; then
      echo "$output" >&2
    fi
    echo "ssh session ended before /opt/data validation completed on $target_host" >&2
    return 24
  fi

  source_line="$(grep -m1 '__OPT_DATA_SOURCE__ ' <<<"$output" || true)"
  if [[ -n "$source_line" ]]; then
    echo "[$target_host] /opt/data mounted from ${source_line#__OPT_DATA_SOURCE__ }" >&2
    return 0
  fi

  if grep -q '__OPT_DATA_MISSING__' <<<"$output"; then
    echo "/opt/data is not mounted separately on $target_host yet" >&2
    return 22
  fi

  if [[ -n "$output" ]]; then
    echo "$output" >&2
  fi
  echo "timed out validating /opt/data on $target_host" >&2
  return 23
}

wait_for_data_mount() {
  local target_host="$1"
  local timeout_seconds="$2"
  local deadline=$((SECONDS + timeout_seconds))
  local last_notice=0
  local rc=1

  while (( SECONDS < deadline )); do
    if check_data_mount "$target_host"; then
      return 0
    fi
    rc=$?
    if (( SECONDS - last_notice >= 60 )); then
      log "waiting for /opt/data to be mounted separately on ${target_host}"
      last_notice=$SECONDS
    fi
    pause_with_log 15 "retrying /opt/data mount validation on ${target_host}"
  done

  return "$rc"
}

log "waiting for SSH because /dev/vdb validation requires an interactive login"
wait_for_port "$host" 22 "$ssh_timeout_seconds"

log "connecting over SSH for interactive first-boot handling"
expect_rc=0
set +e
HOST="$host" USERNAME="$user" PASSWORD="$password" PREFERRED_DEVICES="$preferred_devices" expect <<'EOF'
set timeout 1200
set bootstrap_handled 0
set cli_enter_attempts 0
set prompt_probe_attempts 0

proc fail {msg} {
  puts stderr $msg
  exit 1
}

proc pick_device {buffer preferred_csv} {
  array set choices {}
  foreach line [split $buffer "\n"] {
    if {[regexp {^\s*([0-9]+)\)\s*([[:alnum:]_.\/-]+)\s*$} $line -> number device]} {
      set choices($device) $number
    }
  }

  foreach device [split $preferred_csv ","] {
    if {[info exists choices($device)]} {
      return $choices($device)
    }
  }

  foreach device [array names choices] {
    if {![regexp {[0-9]+$} $device] && $device ni {"sr0" "vda" "sda" "vda1" "sda1"}} {
      return $choices($device)
    }
  }

  return ""
}

set host $env(HOST)
set username $env(USERNAME)
set password $env(PASSWORD)
set preferred_devices $env(PREFERRED_DEVICES)

spawn ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -o LogLevel=ERROR ${username}@${host}

expect {
  -nocase -re {are you sure you want to continue connecting.*} {
    send -- "yes\r"
    exp_continue
  }
  -nocase -re {password:} {
    send -- "$password\r"
    exp_continue
  }
  -nocase -re {you must set an initial admin password} {
    set bootstrap_handled 1
    send_user -- "\[$host\] setting initial admin password\n"
    expect -nocase -re {password:}
    send -- "$password\r"
    expect -nocase -re {re-enter password:}
    send -- "$password\r"
    exp_continue
  }
  -nocase -re {select persona for vmanage.*:} {
    set bootstrap_handled 1
    send_user -- "\[$host\] selecting vManage persona\n"
    send -- "1\r"
    expect -nocase -re {are you sure\?.*}
    send -- "y\r"
    exp_continue
  }
  -nocase -re {select storage device to use:} {
    set bootstrap_handled 1
    set choice [pick_device $expect_out(buffer) $preferred_devices]
    if {$choice eq ""} {
      fail "Unable to select the vManage storage device from:\n$expect_out(buffer)"
    }
    send_user -- "\[$host\] selecting storage option $choice\n"
    send -- "$choice\r"
    exp_continue
  }
  -nocase -re {would you like to format .*\(y/n\):} {
    set bootstrap_handled 1
    send_user -- "\[$host\] confirming data-disk format\n"
    send -- "y\r"
    exp_continue
  }
  -re {(?m)^[^\n]*\$\s*$} {
    if {$cli_enter_attempts < 3} {
      incr cli_enter_attempts
      send_user -- "\[$host\] entering Viptela CLI from Linux shell\n"
      send -- "cli\r"
      exp_continue
    }
    fail "Timed out entering the vManage CLI from the Linux shell on $host"
  }
  -re {mke2fs .*|Discarding device blocks:|Creating filesystem with .*|Filesystem UUID:|Superblock backups stored on blocks:|Allocating group tables:|Writing inode tables:} {
    exp_continue
  }
  -nocase -re {extracting vmanage extra-packages|vmanage extra-package extraction complete|package extraction complete|reboot now!|the system is going down for reboot now!} {
    exp_continue
  }
  -re {(?m)^vmanage[#>]\s*$} {
    if {!$bootstrap_handled && $prompt_probe_attempts < 2} {
      incr prompt_probe_attempts
      send_user -- "\[$host\] probing CLI prompt for pending first-boot questions\n"
      after 2000
      send -- "\r"
      exp_continue
    }
    if {$bootstrap_handled} {
      send_user -- "\[$host\] interactive first-boot questions completed\n"
      exit 0
    }
    send_user -- "\[$host\] no interactive first-boot questions detected\n"
    exit 10
  }
  -re {(?m)^[[:alnum:]_.-]+[#>]\s*$} {
    if {!$bootstrap_handled && $prompt_probe_attempts < 2} {
      incr prompt_probe_attempts
      send_user -- "\[$host\] probing CLI prompt for pending first-boot questions\n"
      after 2000
      send -- "\r"
      exp_continue
    }
    if {$bootstrap_handled} {
      send_user -- "\[$host\] interactive first-boot questions completed\n"
      exit 0
    }
    send_user -- "\[$host\] no interactive first-boot questions detected\n"
    exit 10
  }
  eof {
    exit 0
  }
  timeout {
    fail "Timed out while waiting for the vManage first-boot prompt on $host"
  }
}
EOF
expect_rc=$?
set -e

if [[ $expect_rc -eq 10 ]]; then
  log "no interactive first-boot prompt detected; validating /opt/data before proceeding"
  wait_for_data_mount "$host" "$data_mount_timeout_seconds"
  if [[ "$wait_for_https_after_init" == "1" ]]; then
    log "validated /opt/data; waiting for HTTPS because VMANAGE_INIT_WAIT_FOR_HTTPS=1"
    wait_for_https "$https_host" "$https_timeout_seconds"
    log "HTTPS is available"
  else
    log "/opt/data validation completed; proceeding without waiting for HTTPS"
  fi
  exit 0
elif [[ $expect_rc -ne 0 ]]; then
  exit "$expect_rc"
fi

log "waiting for the expected reboot after disk formatting"
if wait_for_port_down "$host" 22 300; then
  log "device is rebooting; waiting for SSH to return"
  wait_for_port "$host" 22 "$reboot_timeout_seconds"
fi

log "validating /opt/data after the first-boot reboot"
wait_for_data_mount "$host" "$data_mount_timeout_seconds"

if [[ "$wait_for_https_after_init" == "1" ]]; then
  log "/opt/data validation succeeded; waiting for vManage HTTPS because VMANAGE_INIT_WAIT_FOR_HTTPS=1"
  wait_for_https "$https_host" "$https_timeout_seconds"
  log "HTTPS is available"
else
  log "SSH is back and /opt/data is mounted; proceeding without waiting for HTTPS"
fi

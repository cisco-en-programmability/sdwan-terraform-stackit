#!/usr/bin/env python3
"""Add vSmart and vBond controllers to vManage after cert installation.

This script is intended to run manually after:
1. `terraform apply`
2. `post_deploy_controllers.py`
3. `stackit_cluster_certificate.py` or `bootstrap_vmanage_cluster.py`

Flow follows the adab controller bring-up workflow:
- add vSmarts first with `generateCSR=false`
- add vBonds next with `generateCSR=false`
- sync certificates to vBonds via `/dataservice/certificate/vsmart/list`
- wait until vManage reports the targeted controllers as reachable with expected
  control connections up

Notes:
- This is now mostly a legacy helper. The active published flow uses
  `stackit_cluster_certificate.py` for cluster formation + controller add +
  certificate enrollment together.
- The script reads `controller_inventory` from Terraform outputs in the module
  directory. Use `--module-dir` if the repo was copied elsewhere.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from bootstrap_vmanage_cluster import VManageClient, VManageError, parse_tfvars_string, terraform_output

MODULE_DIR = Path(__file__).resolve().parents[1]


def log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


def sleep_with_log(seconds: int, reason: str) -> None:
    log(f"sleeping {seconds}s: {reason}")
    time.sleep(seconds)


def choose_management_address(node: Dict[str, Any]) -> str:
    value = node.get("management_public_ip")
    if isinstance(value, str) and value:
        return value
    value = node.get("transport_public_ip")
    if isinstance(value, str) and value:
        return value
    raise RuntimeError(f"No public IP available for {node.get('hostname')}")


def to_int(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


def extract_data_list(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    return []


def build_config_from_terraform(
    module_dir: Path,
    username: str,
    password: str,
    selected: Optional[set[str]],
    poll_interval_seconds: int,
    controller_ready_timeout_seconds: int,
) -> Dict[str, Any]:
    inventory = terraform_output(module_dir, "controller_inventory")
    if not isinstance(inventory, dict):
        raise RuntimeError("controller_inventory output is not a map")

    vmanage_nodes: List[Dict[str, Any]] = []
    controller_nodes: List[Dict[str, Any]] = []
    for key, raw in sorted(inventory.items()):
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role", ""))
        if role == "vmanage":
            vmanage_nodes.append(
                {
                    "key": str(key),
                    "hostname": str(raw.get("hostname", key)),
                    "management_url": f"https://{choose_management_address(raw)}",
                }
            )
            continue
        if role not in {"vsmart", "vbond"}:
            continue
        if selected and key not in selected:
            continue
        transport_ip = raw.get("transport_ip")
        system_ip = raw.get("system_ip")
        if not isinstance(transport_ip, str) or not transport_ip:
            raise RuntimeError(f"{key} is missing transport_ip in controller_inventory")
        if not isinstance(system_ip, str) or not system_ip:
            raise RuntimeError(f"{key} is missing system_ip in controller_inventory")
        controller_nodes.append(
            {
                "key": str(key),
                "hostname": str(raw.get("hostname", key)),
                "role": role,
                "system_ip": system_ip,
                "device_ip": transport_ip,
            }
        )

    if not vmanage_nodes:
        raise RuntimeError("No vManage nodes found in controller_inventory")
    if not controller_nodes:
        raise RuntimeError("No vBond or vSmart nodes selected from controller_inventory")

    primary = vmanage_nodes[0]
    return {
        "username": username,
        "password": password,
        "primary_url": primary["management_url"],
        "primary_hostname": primary["hostname"],
        "poll_interval_seconds": poll_interval_seconds,
        "controller_ready_timeout_seconds": controller_ready_timeout_seconds,
        "nodes": controller_nodes,
    }


def list_registered_controllers(client: VManageClient) -> List[Dict[str, Any]]:
    return extract_data_list(client.request("GET", "/dataservice/system/device/controllers"))


def find_registered_controller(
    entries: Iterable[Dict[str, Any]], node: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    for entry in entries:
        host_name = str(entry.get("host-name") or entry.get("host_name") or "")
        device_ip = str(entry.get("deviceIP") or entry.get("device_ip") or "")
        system_ip = str(entry.get("system-ip") or entry.get("system_ip") or "")
        if (
            host_name == node["hostname"]
            or device_ip == node["device_ip"]
            or system_ip == node["system_ip"]
        ):
            return entry
    return None


def add_controller_payload(node: Dict[str, Any], username: str, password: str) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "deviceIP": node["device_ip"],
        "username": username,
        "password": password,
        "generateCSR": False,
        "personality": node["role"],
    }
    if node["role"] == "vsmart":
        payload["protocol"] = "DTLS"
    return payload


def add_missing_controllers(config: Dict[str, Any]) -> None:
    client = VManageClient(config["primary_url"], config["username"], config["password"])
    role_order = ("vsmart", "vbond")
    for role in role_order:
        for node in [item for item in config["nodes"] if item["role"] == role]:
            registered = list_registered_controllers(client)
            if find_registered_controller(registered, node):
                log(f"{node['hostname']} already exists in vManage as {node['device_ip']}")
                continue
            payload = add_controller_payload(node, config["username"], config["password"])
            log(
                f"Adding {role} {node['hostname']} to {config['primary_hostname']} "
                f"using transport IP {node['device_ip']} with generateCSR=false"
            )
            client.request("POST", "/dataservice/system/device", payload)
            wait_for_controller_registration(
                config,
                node,
                timeout=config["controller_ready_timeout_seconds"],
                interval=config["poll_interval_seconds"],
            )


def trigger_certificate_sync(config: Dict[str, Any]) -> Optional[str]:
    client = VManageClient(config["primary_url"], config["username"], config["password"])
    response = client.request("POST", "/dataservice/certificate/vsmart/list")
    if isinstance(response, dict):
        task_id = response.get("id")
        if isinstance(task_id, str) and task_id:
            log(f"Triggered controller certificate sync task {task_id}")
            return task_id
    log("Certificate sync API did not return a task id; continuing with readiness polling")
    return None


def task_is_success(payload: Any) -> bool:
    entries = extract_data_list(payload)
    if not entries:
        return False
    statuses = [str(entry.get("status", "")).lower() for entry in entries]
    if all("success" in status for status in statuses):
        return True
    if any("fail" in status for status in statuses):
        raise RuntimeError(f"Controller certificate sync failed: {entries}")
    return False


def wait_for_task(
    config: Dict[str, Any],
    task_id: str,
    timeout: int,
    interval: int,
) -> None:
    client = VManageClient(config["primary_url"], config["username"], config["password"])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = client.request("GET", f"/dataservice/device/action/status/{task_id}")
        if task_is_success(payload):
            log(f"Controller certificate sync task {task_id} completed successfully")
            return
        sleep_with_log(interval, f"waiting for controller certificate sync task {task_id}")
    raise TimeoutError(f"Timed out waiting for controller certificate sync task {task_id}")


def wait_for_controller_registration(
    config: Dict[str, Any],
    node: Dict[str, Any],
    timeout: int,
    interval: int,
) -> None:
    client = VManageClient(config["primary_url"], config["username"], config["password"])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        match = find_registered_controller(list_registered_controllers(client), node)
        if match:
            validity = str(match.get("validity") or "").lower()
            state = str(match.get("state") or match.get("deviceState") or "")
            log(
                f"{node['hostname']} is registered in vManage "
                f"(validity={validity or 'unknown'}, state={state or 'unknown'})"
            )
            return
        sleep_with_log(interval, f"waiting for {node['hostname']} to appear in vManage controller inventory")
    raise TimeoutError(f"Timed out waiting for {node['hostname']} to appear in vManage controller inventory")


def get_reachability_rows(client: VManageClient, personality: str) -> List[Dict[str, Any]]:
    return extract_data_list(client.request("GET", f"/dataservice/device/reachable?personality={personality}"))


def controller_is_up(row: Dict[str, Any]) -> bool:
    reachability = str(row.get("reachability") or "").lower()
    if reachability != "reachable":
        return False

    expected_control_raw = row.get("controlConnections")
    current_control_raw = row.get("controlConnectionsUp")
    if expected_control_raw is not None and current_control_raw is not None:
        if to_int(current_control_raw) != to_int(expected_control_raw):
            return False

    expected_bfd_raw = row.get("bfdSessions")
    current_bfd_raw = row.get("bfdSessionsUp")
    if expected_bfd_raw is not None and current_bfd_raw is not None:
        if to_int(current_bfd_raw) != to_int(expected_bfd_raw):
            return False

    return True


def wait_for_controllers_up(config: Dict[str, Any], timeout: int, interval: int) -> None:
    client = VManageClient(config["primary_url"], config["username"], config["password"])
    deadline = time.monotonic() + timeout
    last_status: List[str] = []
    while time.monotonic() < deadline:
        last_status = []
        all_up = True
        registered_rows = list_registered_controllers(client)
        for role in ("vsmart", "vbond"):
            health_rows = get_reachability_rows(client, role)
            for node in [item for item in config["nodes"] if item["role"] == role]:
                registered = find_registered_controller(registered_rows, node)
                if not registered:
                    all_up = False
                    last_status.append(f"{node['hostname']} missing from controller inventory")
                    continue
                validity = str(registered.get("validity") or "").lower()
                if validity and validity != "valid":
                    all_up = False
                    last_status.append(f"{node['hostname']} validity={registered.get('validity')}")
                    continue
                match = find_registered_controller(health_rows, node)
                if not match:
                    all_up = False
                    last_status.append(f"{node['hostname']} missing from {role} reachability view")
                    continue
                if not controller_is_up(match):
                    all_up = False
                    last_status.append(
                        f"{node['hostname']} reachability={match.get('reachability')} "
                        f"control={match.get('controlConnectionsUp')}/{match.get('controlConnections')} "
                        f"bfd={match.get('bfdSessionsUp')}/{match.get('bfdSessions')}"
                    )
                    continue
                log(
                    f"{node['hostname']} is UP in vManage "
                    f"(reachability={match.get('reachability')}, "
                    f"control={match.get('controlConnectionsUp')}/{match.get('controlConnections')}, "
                    f"bfd={match.get('bfdSessionsUp')}/{match.get('bfdSessions')})"
                )
        if all_up:
            return
        sleep_with_log(interval, "waiting for vBond and vSmart controllers to report UP in vManage")
    detail = "; ".join(last_status) if last_status else "no controller status returned"
    raise TimeoutError(f"Timed out waiting for vBond/vSmart controllers to come UP in vManage: {detail}")


def print_plan(config: Dict[str, Any]) -> None:
    print("Planned controller registration:", flush=True)
    print(f"  primary vManage: {config['primary_hostname']} via {config['primary_url']}", flush=True)
    for node in config["nodes"]:
        print(
            f"  add {node['role']}: {node['hostname']} using transport IP {node['device_ip']}",
            flush=True,
        )


def confirm(auto_approve: bool) -> None:
    if auto_approve:
        return
    response = input("Type 'yes' to continue adding controllers to vManage: ").strip()
    if response != "yes":
        raise RuntimeError("Controller registration cancelled by operator")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module-dir", default=str(MODULE_DIR), help="Path to the Terraform module directory.")
    parser.add_argument("--username", default="admin", help="vManage username. Defaults to admin.")
    parser.add_argument("--password", default="", help="vManage password. Defaults to admin_password in terraform.tfvars.")
    parser.add_argument("--controllers", default="", help="Comma-separated controller keys to process, for example vsmart01,vbond01.")
    parser.add_argument("--poll-interval-seconds", type=int, default=30, help="Polling interval for readiness checks.")
    parser.add_argument("--controller-ready-timeout-seconds", type=int, default=3600, help="Timeout for controllers to show UP in vManage.")
    parser.add_argument("--task-timeout-seconds", type=int, default=1800, help="Timeout for the vManage certificate sync task.")
    parser.add_argument("--yes", action="store_true", help="Skip the operator confirmation prompt.")
    parser.add_argument("--verify-only", action="store_true", help="Only verify that the selected controllers are already UP in vManage.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    module_dir = Path(args.module_dir).resolve()
    tfvars_path = module_dir / "terraform.tfvars"
    password = args.password or parse_tfvars_string(tfvars_path, "admin_password")
    selected = {item.strip() for item in args.controllers.split(",") if item.strip()} or None
    config = build_config_from_terraform(
        module_dir=module_dir,
        username=args.username,
        password=password,
        selected=selected,
        poll_interval_seconds=args.poll_interval_seconds,
        controller_ready_timeout_seconds=args.controller_ready_timeout_seconds,
    )

    print_plan(config)

    try:
        if args.verify_only:
            wait_for_controllers_up(
                config,
                timeout=config["controller_ready_timeout_seconds"],
                interval=config["poll_interval_seconds"],
            )
            log("Selected vBond/vSmart controllers are already UP in vManage")
            return 0

        confirm(args.yes)
        add_missing_controllers(config)
        task_id = trigger_certificate_sync(config)
        if task_id:
            wait_for_task(
                config,
                task_id=task_id,
                timeout=args.task_timeout_seconds,
                interval=config["poll_interval_seconds"],
            )
        wait_for_controllers_up(
            config,
            timeout=config["controller_ready_timeout_seconds"],
            interval=config["poll_interval_seconds"],
        )
        log("vBond and vSmart controller registration completed successfully")
        return 0
    except (RuntimeError, TimeoutError, VManageError) as exc:
        log(f"Controller registration failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

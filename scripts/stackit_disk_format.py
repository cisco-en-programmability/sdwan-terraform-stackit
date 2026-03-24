#!/usr/bin/env python3
"""Handle the interactive vManage /dev/vdb first-boot flow in parallel.

This stage only passes when each selected vManage node confirms that
/opt/data is mounted as a separate filesystem after any first-boot reboot.

Notes:
- The script reads `controller_inventory` from Terraform outputs and targets
  only `vManage` nodes.
- Use `--module-dir` if the Terraform module is in a different checkout.
- This script is safe to rerun; it treats `/opt/data` as the success signal.
- This is the published disk-format entry point used after Terraform bring-up.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from bootstrap_vmanage_cluster import parse_tfvars_string, terraform_output


MODULE_DIR = Path(__file__).resolve().parents[1]
PREFERRED_VMANAGE_DEVICES = "vdb,sdb,xvdb,nvme1n1,hdc,sdc"


def log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


def choose_public_address(node: Dict[str, Any]) -> str:
    for key in ("management_public_ip", "transport_public_ip"):
        value = node.get(key)
        if isinstance(value, str) and value:
            return value
    raise RuntimeError(f"No public IP available for {node.get('hostname')}")


def build_vmanage_nodes(module_dir: Path, selected: Optional[set[str]]) -> List[Dict[str, str]]:
    inventory = terraform_output(module_dir, "controller_inventory")
    if not isinstance(inventory, dict):
        raise RuntimeError("controller_inventory output is not a map")

    nodes: List[Dict[str, str]] = []
    for key, raw in sorted(inventory.items()):
        if not isinstance(raw, dict):
            continue
        if str(raw.get("role", "")) != "vmanage":
            continue
        if selected and key not in selected:
            continue
        nodes.append(
            {
                "key": str(key),
                "hostname": str(raw.get("hostname", key)),
                "public_ip": choose_public_address(raw),
            }
        )

    if not nodes:
        raise RuntimeError("No vManage nodes selected from controller_inventory")
    return nodes


def run_vmanage_disk_init(host: str, password: str, username: str) -> None:
    subprocess.run(
        [
            "bash",
            str(MODULE_DIR / "scripts" / "init_vmanage_firstboot.sh"),
            host,
            password,
            username,
            PREFERRED_VMANAGE_DEVICES,
            host,
        ],
        check=True,
        text=True,
    )


def parse_selected(values: Optional[str]) -> Optional[set[str]]:
    if not values:
        return None
    return {item.strip() for item in values.split(",") if item.strip()}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the vManage /dev/vdb first-boot helper across one or more vManage nodes."
    )
    parser.add_argument(
        "--module-dir",
        default=str(MODULE_DIR),
        help="Terraform module directory. Defaults to the repo root.",
    )
    parser.add_argument(
        "--username",
        default="admin",
        help="Linux/vManage login username. Defaults to admin.",
    )
    parser.add_argument(
        "--password",
        default=None,
        help="Controller password. Defaults to admin_password from terraform.tfvars.",
    )
    parser.add_argument(
        "--controllers",
        default=None,
        help="Comma-separated subset such as vmanage01,vmanage02. Defaults to all vManage nodes.",
    )
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=3,
        help="Maximum parallel vManage first-boot workers. Defaults to 3.",
    )
    args = parser.parse_args()

    module_dir = Path(args.module_dir).resolve()
    tfvars_path = module_dir / "terraform.tfvars"
    password = args.password or parse_tfvars_string(tfvars_path, "admin_password")
    selected = parse_selected(args.controllers)
    nodes = build_vmanage_nodes(module_dir, selected)

    failures: List[str] = []
    max_workers = max(1, min(args.max_parallel, len(nodes)))
    log(f"Formatting workflow will run for {len(nodes)} vManage node(s) with {max_workers} parallel worker(s)")
    log("Each worker will validate that /opt/data is mounted separately before reporting success")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(run_vmanage_disk_init, node["public_ip"], password, args.username): node
            for node in nodes
        }
        for future in as_completed(future_map):
            node = future_map[future]
            try:
                future.result()
                log(f"{node['key']} ({node['hostname']}) finished /dev/vdb first-boot handling")
            except Exception as exc:  # pragma: no cover - surfaced to operator
                message = f"{node['key']} ({node['hostname']}): {exc}"
                failures.append(message)
                log(message)

    if failures:
        print("\nFailed nodes:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    log("All selected vManage nodes completed /dev/vdb first-boot handling")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

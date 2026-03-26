#!/usr/bin/env python3
"""Run the published post-deploy controller workflow after disk formatting.

This wrapper keeps the operator flow simple:
1. run Terraform
2. run `stackit_disk_format.py`
3. run this script

The wrapper first performs 3-node vManage cluster formation and then runs the
controller certificate workflow. It intentionally delegates to the underlying
implementation scripts so the lower-level tools remain available for debugging.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional


SCRIPTS_DIR = Path(__file__).resolve().parent
CLUSTER_SCRIPT = SCRIPTS_DIR / "bootstrap_vmanage_cluster.py"
CERT_SCRIPT = SCRIPTS_DIR / "cert_api_script.py"


def run_step(cmd: List[str], label: str) -> int:
    print(f"==> {label}", flush=True)
    completed = subprocess.run(cmd, text=True)
    if completed.returncode != 0:
        print(f"{label} failed with exit code {completed.returncode}", file=sys.stderr, flush=True)
    return completed.returncode


def append_optional_arg(cmd: List[str], flag: str, value: str | None) -> None:
    if value is None:
        return
    cmd.extend([flag, value])


def discover_primary_vmanage_url(module_dir: str) -> Optional[str]:
    try:
        completed = subprocess.run(
            ["terraform", f"-chdir={module_dir}", "output", "-json", "controller_inventory"],
            text=True,
            capture_output=True,
            check=True,
        )
        inventory = json.loads(completed.stdout)
        if not isinstance(inventory, dict):
            return None
        for key in sorted(inventory):
            node = inventory.get(key)
            if not isinstance(node, dict) or str(node.get("role", "")) != "vmanage":
                continue
            for field in ("management_public_ip", "transport_public_ip"):
                value = str(node.get(field) or "").strip()
                if value:
                    return f"https://{value}"
    except Exception:
        return None
    return None


def sleep_with_message(seconds: int, reason: str) -> None:
    print(f"==> sleeping {seconds} seconds: {reason}", flush=True)
    time.sleep(seconds)


def wait_for_cluster_stabilization(seconds: int) -> None:
    if seconds <= 0:
        return
    print(
        f"==> post-cluster stabilization wait ({seconds} seconds)",
        flush=True,
    )
    print(
        "The vManage cluster is up, but certificate enrollment works better after an additional stabilization window.",
        flush=True,
    )
    sleep_with_message(seconds, "allowing the 3-node vManage cluster to stabilize before certificate enrollment")


def confirm_certificate_stage(primary_vmanage_url: Optional[str]) -> None:
    print(
        "Certificate enrollment will now begin. This step can trigger controller serial-list and certificate pushes across the cluster.",
        flush=True,
    )
    if primary_vmanage_url:
        print(
            f"Before continuing, open {primary_vmanage_url} and go to Administration > Cluster Management.",
            flush=True,
        )
    else:
        print(
            "Before continuing, open the primary vManage URL and go to Administration > Cluster Management.",
            flush=True,
        )
    print(
        "Confirm the cluster is Ready and the Service Reachability tab shows all nodes healthy and reachable.",
        flush=True,
    )
    response = input("Type 'yes' to continue with controller certificate enrollment: ").strip()
    if response != "yes":
        raise RuntimeError("Controller certificate enrollment cancelled by operator")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run vManage cluster formation followed by controller certificate enrollment."
    )
    parser.add_argument("--module-dir", default="", help="Terraform module directory. Defaults to the repo root.")
    parser.add_argument("--username", default="admin", help="vManage username. Defaults to admin.")
    parser.add_argument("--password", default=None, help="Controller password. Defaults to admin_password from terraform.tfvars.")
    parser.add_argument("--yes", action="store_true", help="Skip the cluster confirmation prompt.")
    parser.add_argument("--controllers", default=None, help="Optional subset for certificate enrollment, such as vbond01,vbond02.")
    parser.add_argument(
        "--controller-certificate-method",
        choices=("cisco_pki", "enterprise_local"),
        default=None,
        help="Certificate flow override. Defaults to controller_certificate_method from terraform.tfvars.",
    )
    parser.add_argument("--artifacts-dir", default=None, help="Artifact directory for downloaded CSRs and signed certs.")
    parser.add_argument("--ca-cert", default=None, help="Controller root CA certificate path for enterprise_local.")
    parser.add_argument("--ca-key", default=None, help="Controller root CA private key path for enterprise_local.")
    parser.add_argument(
        "--smart-account-preconfigured",
        action="store_true",
        help="Skip the manual Cisco Services Registration prompt and assume it is already configured on vManage.",
    )
    parser.add_argument("--poll-interval-seconds", type=int, default=10, help="Polling interval used by both workflow stages.")
    parser.add_argument("--server-ready-timeout-seconds", type=int, default=7200, help="Timeout for vManage HTTPS and server-ready checks.")
    parser.add_argument("--cluster-ready-timeout-seconds", type=int, default=10800, help="Timeout for cluster convergence.")
    parser.add_argument("--ready-timeout-seconds", type=int, default=2400, help="Timeout for certificate registration and enrollment waits.")
    parser.add_argument(
        "--post-cluster-delay-seconds",
        type=int,
        default=300,
        help="Additional stabilization delay after cluster formation and before certificate enrollment. Defaults to 300 seconds.",
    )
    args = parser.parse_args()

    module_dir = args.module_dir or str(Path(__file__).resolve().parents[1])
    primary_vmanage_url = discover_primary_vmanage_url(module_dir)

    cluster_cmd = [
        sys.executable,
        str(CLUSTER_SCRIPT),
        "--module-dir",
        module_dir,
        "--username",
        args.username,
        "--poll-interval-seconds",
        str(args.poll_interval_seconds),
        "--server-ready-timeout-seconds",
        str(args.server_ready_timeout_seconds),
        "--cluster-ready-timeout-seconds",
        str(args.cluster_ready_timeout_seconds),
    ]
    append_optional_arg(cluster_cmd, "--password", args.password)
    if args.yes:
        cluster_cmd.append("--yes")

    if run_step(cluster_cmd, "vManage cluster formation") != 0:
        return 1

    wait_for_cluster_stabilization(args.post_cluster_delay_seconds)
    confirm_certificate_stage(primary_vmanage_url)

    cert_cmd = [
        sys.executable,
        str(CERT_SCRIPT),
        "--module-dir",
        module_dir,
        "--username",
        args.username,
        "--poll-interval-seconds",
        str(args.poll_interval_seconds),
        "--ready-timeout-seconds",
        str(args.ready_timeout_seconds),
        "--yes",
    ]
    append_optional_arg(cert_cmd, "--password", args.password)
    append_optional_arg(cert_cmd, "--controllers", args.controllers)
    append_optional_arg(cert_cmd, "--controller-certificate-method", args.controller_certificate_method)
    append_optional_arg(cert_cmd, "--artifacts-dir", args.artifacts_dir)
    append_optional_arg(cert_cmd, "--ca-cert", args.ca_cert)
    append_optional_arg(cert_cmd, "--ca-key", args.ca_key)
    if args.smart_account_preconfigured:
        cert_cmd.append("--smart-account-preconfigured")

    return run_step(cert_cmd, "controller certificate enrollment")


if __name__ == "__main__":
    raise SystemExit(main())

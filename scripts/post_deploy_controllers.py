#!/usr/bin/env python3
"""Legacy direct-device controller certificate bootstrap helper.

Notes:
- This script is kept as a fallback and reference path.
- The active published workflow uses `stackit_cluster_certificate.py` instead.
- It still reads Terraform outputs from the module directory and supports
  `--module-dir` for copied checkouts.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path


MODULE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACTS_DIR = MODULE_DIR / "artifacts" / "controller-certs"
DEFAULT_CA_DIR = MODULE_DIR / "certs" / "controllers"
PREFERRED_VMANAGE_DEVICES = "vdb,sdb,xvdb,nvme1n1,hdc,sdc"


def run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            text=True,
            capture_output=True,
            check=check,
        )
    except subprocess.CalledProcessError as exc:
        stdout = exc.stdout.strip()
        stderr = exc.stderr.strip()
        details = [f"Command {exc.cmd!r} failed with exit status {exc.returncode}."]
        if stdout:
            details.append(f"stdout:\n{stdout}")
        if stderr:
            details.append(f"stderr:\n{stderr}")
        raise RuntimeError("\n".join(details)) from exc


def sleep_with_log(seconds: int, reason: str) -> None:
    print(f"[post-deploy] sleeping {seconds}s: {reason}", flush=True)
    time.sleep(seconds)


def parse_tfvars_string(tfvars_path: Path, key: str) -> str:
    pattern = re.compile(rf"^{re.escape(key)}\s*=\s*\"([^\"]*)\"", re.MULTILINE)
    match = pattern.search(tfvars_path.read_text())
    if not match:
        raise RuntimeError(f"Unable to find {key} in {tfvars_path}")
    return match.group(1)


def parse_tfvars_string_default(tfvars_path: Path, key: str, default: str) -> str:
    pattern = re.compile(rf"^{re.escape(key)}\s*=\s*\"([^\"]*)\"", re.MULTILINE)
    match = pattern.search(tfvars_path.read_text())
    return match.group(1) if match else default


def parse_tfvars_number_default(tfvars_path: Path, key: str, default: int) -> int:
    pattern = re.compile(rf"^{re.escape(key)}\s*=\s*(\d+)", re.MULTILINE)
    match = pattern.search(tfvars_path.read_text())
    return int(match.group(1)) if match else default


def terraform_output(module_dir: Path, name: str) -> object:
    result = run(["terraform", f"-chdir={module_dir}", "output", "-json", name])
    return json.loads(result.stdout)


def ensure_root_ca(ca_cert_path: Path, ca_key_path: Path, organization_name: str) -> None:
    if ca_cert_path.exists() and ca_key_path.exists():
      return

    ca_cert_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "bash",
        str(MODULE_DIR / "scripts" / "generate_controller_root_ca.sh"),
        "--output-dir",
        str(ca_cert_path.parent),
        "--org",
        organization_name,
        "--root-cn",
        f"{organization_name} Controller Root CA",
        "--valid-days",
        "3650",
    ]
    run(cmd)


def wait_for_port(host: str, port: int, timeout_seconds: int, *, want_open: bool) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(2)
            try:
                sock.connect((host, port))
                is_open = True
            except OSError:
                is_open = False
        if is_open == want_open:
            return
        sleep_with_log(5, f"waiting for {host}:{port} to become {'open' if want_open else 'closed'}")
    state = "open" if want_open else "closed"
    raise RuntimeError(f"Timed out waiting for {host}:{port} to become {state}")


def run_expect(script: str, env: dict[str, str]) -> str:
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".exp") as handle:
        handle.write(script)
        path = Path(handle.name)
    try:
        result = run(["/usr/bin/expect", str(path)], env={**os.environ, **env})
        return result.stdout
    finally:
        path.unlink(missing_ok=True)


def ssh_exec(host: str, username: str, password: str, remote_cmd: str, *, timeout: int = 600) -> str:
    script = textwrap.dedent(
        r"""
        set timeout $env(TIMEOUT)
        log_user 0
        spawn ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 $env(USERNAME)@$env(HOST)
        expect {
          -nocase -re {are you sure you want to continue connecting.*} {
            send -- "yes\r"
            exp_continue
          }
          -nocase -re {password:} {
            send -- "$env(PASSWORD)\r"
            exp_continue
          }
          -re {(?m)^[^\n]*[#>]\s*$} {}
          timeout {
            puts stderr "Timed out connecting to $env(HOST)"
            exit 124
          }
        }

        send -- "$env(REMOTE_CMD)\r"
        expect {
          -re {(?m)^[^\n]*[#>]\s*$} {
            puts $expect_out(buffer)
          }
          timeout {
            puts stderr "Timed out running remote command on $env(HOST)"
            exit 124
          }
        }

        send -- "exit\r"
        expect eof
        """
    )
    return run_expect(
        script,
        {
            "HOST": host,
            "USERNAME": username,
            "PASSWORD": password,
            "REMOTE_CMD": remote_cmd,
            "TIMEOUT": str(timeout),
        },
    )


def scp_from(host: str, username: str, password: str, remote_path: str, local_path: Path, *, timeout: int = 600) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    script = textwrap.dedent(
        r"""
        set timeout $env(TIMEOUT)
        log_user 1
        spawn scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 $env(USERNAME)@$env(HOST):$env(REMOTE_PATH) $env(LOCAL_PATH)
        expect {
          -nocase -re {are you sure you want to continue connecting.*} {
            send -- "yes\r"
            exp_continue
          }
          -nocase -re {password:} {
            send -- "$env(PASSWORD)\r"
            exp_continue
          }
          eof {
            catch wait result
            set rc [lindex $result 3]
            exit $rc
          }
          timeout {
            puts stderr "Timed out copying from $env(HOST)"
            exit 124
          }
        }
        """
    )
    run_expect(
        script,
        {
            "HOST": host,
            "USERNAME": username,
            "PASSWORD": password,
            "REMOTE_PATH": remote_path,
            "LOCAL_PATH": str(local_path),
            "TIMEOUT": str(timeout),
        },
    )


def scp_to(host: str, username: str, password: str, local_path: Path, remote_path: str, *, timeout: int = 600) -> None:
    script = textwrap.dedent(
        r"""
        set timeout $env(TIMEOUT)
        log_user 1
        spawn scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 $env(LOCAL_PATH) $env(USERNAME)@$env(HOST):$env(REMOTE_PATH)
        expect {
          -nocase -re {are you sure you want to continue connecting.*} {
            send -- "yes\r"
            exp_continue
          }
          -nocase -re {password:} {
            send -- "$env(PASSWORD)\r"
            exp_continue
          }
          eof {
            catch wait result
            set rc [lindex $result 3]
            exit $rc
          }
          timeout {
            puts stderr "Timed out copying to $env(HOST)"
            exit 124
          }
        }
        """
    )
    run_expect(
        script,
        {
            "HOST": host,
            "USERNAME": username,
            "PASSWORD": password,
            "LOCAL_PATH": str(local_path),
            "REMOTE_PATH": remote_path,
            "TIMEOUT": str(timeout),
        },
    )


def generate_csr(host: str, username: str, password: str, organization_name: str, remote_csr_path: str, *, timeout: int = 1200) -> None:
    script = textwrap.dedent(
        r"""
        set timeout $env(TIMEOUT)
        log_user 1
        spawn ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 $env(USERNAME)@$env(HOST)
        expect {
          -nocase -re {are you sure you want to continue connecting.*} {
            send -- "yes\r"
            exp_continue
          }
          -nocase -re {password:} {
            send -- "$env(PASSWORD)\r"
            exp_continue
          }
          -re {(?m)^[^\n]*[#>]\s*$} {}
          timeout {
            puts stderr "Timed out connecting to $env(HOST)"
            exit 124
          }
        }

        send -- "request csr upload $env(REMOTE_CSR_PATH)\r"
        expect {
          -re {Enter .*organization.*name.*:} {
            send -- "$env(ORG_NAME)\r"
            exp_continue
          }
          -re {Re-enter .*organization.*name.*:} {
            send -- "$env(ORG_NAME)\r"
            exp_continue
          }
          -re {CSR upload successful} {}
          -re {Proceed\?.*} {
            send -- "yes\r"
            exp_continue
          }
          timeout {
            puts stderr "Timed out generating CSR on $env(HOST)"
            exit 124
          }
        }

        send -- "exit\r"
        expect eof
        """
    )
    run_expect(
        script,
        {
            "HOST": host,
            "USERNAME": username,
            "PASSWORD": password,
            "ORG_NAME": organization_name,
            "REMOTE_CSR_PATH": remote_csr_path,
            "TIMEOUT": str(timeout),
        },
    )


def install_root_ca(host: str, username: str, password: str, remote_path: str, *, timeout: int = 900) -> None:
    script = textwrap.dedent(
        r"""
        set timeout $env(TIMEOUT)
        log_user 1
        spawn ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 $env(USERNAME)@$env(HOST)
        expect {
          -nocase -re {are you sure you want to continue connecting.*} {
            send -- "yes\r"
            exp_continue
          }
          -nocase -re {password:} {
            send -- "$env(PASSWORD)\r"
            exp_continue
          }
          -re {(?m)^[^\n]*[#>]\s*$} {}
          timeout {
            puts stderr "Timed out connecting to $env(HOST)"
            exit 124
          }
        }

        send -- "request root-cert-chain install $env(REMOTE_CA_PATH)\r"
        expect {
          -re {Successfully installed} {}
          -re {already installed} {}
          -re {new-roots} {
            send -- "request root-cert-chain install $env(REMOTE_CA_PATH) new-roots\r"
            exp_continue
          }
          -re {Failed} {
            puts stderr $expect_out(buffer)
            exit 1
          }
          timeout {
            puts stderr "Timed out installing root CA on $env(HOST)"
            exit 124
          }
        }

        send -- "exit\r"
        expect eof
        """
    )
    run_expect(
        script,
        {
            "HOST": host,
            "USERNAME": username,
            "PASSWORD": password,
            "REMOTE_CA_PATH": remote_path,
            "TIMEOUT": str(timeout),
        },
    )


def configure_vbond_resolution(
    host: str,
    username: str,
    password: str,
    vbond_hostname: str,
    vbond_ips: list[str],
    vbond_port: int,
    *,
    timeout: int = 900,
) -> None:
    script = textwrap.dedent(
        r"""
        set timeout $env(TIMEOUT)
        log_user 1
        spawn ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 $env(USERNAME)@$env(HOST)
        expect {
          -nocase -re {are you sure you want to continue connecting.*} {
            send -- "yes\r"
            exp_continue
          }
          -nocase -re {password:} {
            send -- "$env(PASSWORD)\r"
            exp_continue
          }
          -re {(?m)^[^\n]*[#>]\s*$} {}
          timeout {
            puts stderr "Timed out connecting to $env(HOST)"
            exit 124
          }
        }

        send -- "config\r"
        expect -re {(?m)^[^\n]*#\s*$}
        send -- "system\r"
        expect -re {(?m)^[^\n]*#\s*$}
        send -- "vbond $env(VBOND_HOSTNAME) port $env(VBOND_PORT)\r"
        expect -re {(?m)^[^\n]*#\s*$}
        send -- "exit\r"
        expect -re {(?m)^[^\n]*#\s*$}
        send -- "vpn 0\r"
        expect -re {(?m)^[^\n]*#\s*$}
        send -- "host $env(VBOND_HOSTNAME) ip $env(VBOND_IPS)\r"
        expect -re {(?m)^[^\n]*#\s*$}
        send -- "exit\r"
        expect -re {(?m)^[^\n]*#\s*$}
        send -- "commit and-quit\r"
        expect {
          -re {Commit complete\.} {}
          -re {(?m)^[^\n]*#\s*$} {}
          -re {Failed} {
            puts stderr $expect_out(buffer)
            exit 1
          }
          timeout {
            puts stderr "Timed out committing vBond hostname config on $env(HOST)"
            exit 124
          }
        }
        send -- "exit\r"
        expect eof
        """
    )
    run_expect(
        script,
        {
            "HOST": host,
            "USERNAME": username,
            "PASSWORD": password,
            "VBOND_HOSTNAME": vbond_hostname,
            "VBOND_IPS": " ".join(vbond_ips),
            "VBOND_PORT": str(vbond_port),
            "TIMEOUT": str(timeout),
        },
    )

def vmanage_has_expected_vbond_resolution(
    host: str,
    username: str,
    password: str,
    vbond_hostname: str,
    vbond_ips: list[str],
) -> bool:
    system_cfg = ssh_exec(host, username, password, "show running-config system | nomore", timeout=120)
    vpn0_cfg = ssh_exec(host, username, password, "show running-config vpn 0 | nomore", timeout=120)

    if f"vbond {vbond_hostname}" not in system_cfg:
        return False
    if f"host {vbond_hostname} ip" not in vpn0_cfg:
        return False
    return all(ip in vpn0_cfg for ip in vbond_ips)


def install_certificate(host: str, username: str, password: str, remote_pem_path: str, *, timeout: int = 900) -> None:
    script = textwrap.dedent(
        r"""
        set timeout $env(TIMEOUT)
        log_user 1
        spawn ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 $env(USERNAME)@$env(HOST)
        expect {
          -nocase -re {are you sure you want to continue connecting.*} {
            send -- "yes\r"
            exp_continue
          }
          -nocase -re {password:} {
            send -- "$env(PASSWORD)\r"
            exp_continue
          }
          -re {(?m)^[^\n]*[#>]\s*$} {}
          timeout {
            puts stderr "Timed out connecting to $env(HOST)"
            exit 124
          }
        }

        send -- "request certificate install $env(REMOTE_PEM_PATH)\r"
        expect {
          -re {Successfully installed the certificate} {}
          -re {Same certificate is already installed} {}
          -re {certificate is not yet valid} {
            send -- "clock set date $env(CLOCK_DATE) time $env(CLOCK_TIME)\r"
            expect -re {(?m)^[^\n]*[#>]\s*$}
            send -- "request certificate install $env(REMOTE_PEM_PATH)\r"
            exp_continue
          }
          -re {Failed to install the certificate} {
            puts stderr $expect_out(buffer)
            exit 1
          }
          timeout {
            puts stderr "Timed out installing certificate on $env(HOST)"
            exit 124
          }
        }

        send -- "exit\r"
        expect eof
        """
    )
    now = time.gmtime()
    run_expect(
        script,
        {
            "HOST": host,
            "USERNAME": username,
            "PASSWORD": password,
            "REMOTE_PEM_PATH": remote_pem_path,
            "CLOCK_DATE": time.strftime("%Y-%m-%d", now),
            "CLOCK_TIME": time.strftime("%H:%M:%S", now),
            "TIMEOUT": str(timeout),
        },
    )


def verify_controller(role: str, host: str, username: str, password: str) -> None:
    command = "show orchestrator local-properties | nomore" if role == "vbond" else "show control local-properties | nomore"
    output = ssh_exec(host, username, password, command, timeout=300)
    if not re.search(r"root-ca-chain-status\s+Installed", output):
        raise RuntimeError(f"{role}@{host}: root-ca-chain-status is not Installed")
    if not re.search(r"certificate-status\s+Installed", output):
        raise RuntimeError(f"{role}@{host}: certificate-status is not Installed")


def sign_csr(ca_cert_path: Path, ca_key_path: Path, csr_path: Path, pem_path: Path, *, days: int = 3650) -> None:
    pem_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        (tmpdir / "certs").mkdir()
        (tmpdir / "index.txt").write_text("")
        # Use a fresh unpredictable hex serial per certificate so every
        # controller cert is unique even though we sign CSRs independently.
        (tmpdir / "serial").write_text(f"{secrets.token_hex(16).upper()}\n")
        openssl_cnf = tmpdir / "openssl.cnf"
        openssl_cnf.write_text(
            textwrap.dedent(
                f"""
                [ ca ]
                default_ca = local_ca

                [ local_ca ]
                dir = {tmpdir}
                database = $dir/index.txt
                new_certs_dir = $dir/certs
                certificate = {ca_cert_path}
                private_key = {ca_key_path}
                serial = $dir/serial
                default_md = sha256
                default_days = {days}
                policy = policy_loose
                x509_extensions = usr_cert
                unique_subject = no
                copy_extensions = copy

                [ policy_loose ]
                countryName = optional
                stateOrProvinceName = optional
                localityName = optional
                organizationName = supplied
                organizationalUnitName = optional
                commonName = supplied
                emailAddress = optional

                [ usr_cert ]
                basicConstraints = CA:FALSE
                keyUsage = digitalSignature, keyEncipherment
                extendedKeyUsage = serverAuth, clientAuth
                subjectKeyIdentifier = hash
                authorityKeyIdentifier = keyid,issuer
                """
            ).strip()
            + "\n"
        )
        run(
            [
                "openssl",
                "ca",
                "-batch",
                "-config",
                str(openssl_cnf),
                "-in",
                str(csr_path),
                "-out",
                str(pem_path),
            ]
        )


def choose_controller_host(node: dict[str, object]) -> str:
    for key in ("management_public_ip", "transport_public_ip"):
        value = node.get(key)
        if isinstance(value, str) and value:
            return value
    raise RuntimeError(f"No public IP available for {node.get('hostname')}")


def run_vmanage_disk_init(host: str, password: str) -> None:
    subprocess.run(
        [
            "bash",
            str(MODULE_DIR / "scripts" / "init_vmanage_firstboot.sh"),
            host,
            password,
            "admin",
            PREFERRED_VMANAGE_DEVICES,
            host,
        ],
        text=True,
        check=True,
    )


def build_selected_nodes(module_dir: Path, selected: set[str] | None) -> list[dict[str, str]]:
    inventory = terraform_output(module_dir, "controller_inventory")
    if not isinstance(inventory, dict):
        raise RuntimeError("controller_inventory output is not a map")

    nodes: list[dict[str, str]] = []
    for key, raw in inventory.items():
        if selected and key not in selected:
            continue
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role", ""))
        if role not in {"vmanage", "vbond", "vsmart"}:
            continue
        nodes.append(
            {
                "key": str(key),
                "hostname": str(raw.get("hostname", key)),
                "role": role,
                "host": choose_controller_host(raw),
            }
        )
    return sorted(nodes, key=lambda item: item["key"])


def build_vbond_resolution(module_dir: Path, tfvars_path: Path) -> tuple[str, list[str], int]:
    inventory = terraform_output(module_dir, "controller_inventory")
    if not isinstance(inventory, dict):
        raise RuntimeError("controller_inventory output is not a map")

    vbond_ips = [
        str(raw.get("transport_ip"))
        for key, raw in sorted(inventory.items())
        if isinstance(raw, dict) and raw.get("role") == "vbond" and raw.get("transport_ip")
    ]
    if not vbond_ips:
        raise RuntimeError("Unable to derive vBond transport IPs from controller_inventory.")

    return (
        parse_tfvars_string_default(tfvars_path, "vbond_hostname", "vbond.vbond"),
        vbond_ips,
        parse_tfvars_number_default(tfvars_path, "vbond_port", 12346),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manual post-deploy controller setup for the STACKIT SD-WAN lab.")
    parser.add_argument("--module-dir", default=str(MODULE_DIR), help="Path to the Terraform module directory.")
    parser.add_argument("--admin-password", default="", help="Controller admin password. Defaults to admin_password in terraform.tfvars.")
    parser.add_argument("--organization-name", default="", help="Cisco SD-WAN organization name. Defaults to organization_name in terraform.tfvars.")
    parser.add_argument("--ca-cert", default=str(DEFAULT_CA_DIR / "root-ca.crt"), help="Path to the shared controller root CA certificate.")
    parser.add_argument("--ca-key", default=str(DEFAULT_CA_DIR / "root-ca.key"), help="Path to the shared controller root CA private key.")
    parser.add_argument("--artifacts-dir", default=str(DEFAULT_ARTIFACTS_DIR), help="Directory where CSRs and signed certs will be stored.")
    parser.add_argument("--controllers", default="", help="Comma-separated controller keys to process, for example vmanage01,vbond01.")
    parser.add_argument("--skip-vmanage-disk-init", action="store_true", help="Skip the vManage /dev/vdb first-boot formatting helper.")
    parser.add_argument("--force-vbond-resolution", action="store_true", help="Force the legacy vManage vBond host/IP update step. New deployments already seed this via cloud-init.")
    parser.add_argument("--skip-cert-install", action="store_true", help="Skip CSR generation, signing, install, and verification.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    module_dir = Path(args.module_dir).resolve()
    tfvars_path = module_dir / "terraform.tfvars"
    admin_password = args.admin_password or parse_tfvars_string(tfvars_path, "admin_password")
    organization_name = args.organization_name or parse_tfvars_string(tfvars_path, "organization_name")
    ca_cert_path = Path(args.ca_cert).resolve()
    ca_key_path = Path(args.ca_key).resolve()
    artifacts_dir = Path(args.artifacts_dir).resolve()
    selected = {item.strip() for item in args.controllers.split(",") if item.strip()} or None

    ensure_root_ca(ca_cert_path, ca_key_path, organization_name)
    nodes = build_selected_nodes(module_dir, selected)
    vbond_hostname, vbond_ips, vbond_port = build_vbond_resolution(module_dir, tfvars_path)
    if not nodes:
        raise RuntimeError("No controller nodes found in Terraform output.")

    username = "admin"

    if not args.skip_vmanage_disk_init:
        vmanage_nodes = [item for item in nodes if item["role"] == "vmanage"]
        if vmanage_nodes:
            for node in vmanage_nodes:
                print(f"[vmanage-init] {node['key']} {node['host']}")

            with ThreadPoolExecutor(max_workers=len(vmanage_nodes)) as executor:
                futures = {
                    executor.submit(run_vmanage_disk_init, node["host"], admin_password): node
                    for node in vmanage_nodes
                }
                for future in as_completed(futures):
                    node = futures[future]
                    future.result()
                    print(f"[vmanage-init-complete] {node['key']} {node['host']}")

    if args.force_vbond_resolution:
        for node in nodes:
            if node["role"] != "vmanage":
                continue
            print(f"[vbond-resolution] {node['key']} {node['host']}")
            wait_for_port(node["host"], 22, 1800, want_open=True)
            if vmanage_has_expected_vbond_resolution(
                node["host"],
                username,
                admin_password,
                vbond_hostname,
                vbond_ips,
            ):
                print(f"[vbond-resolution-skip] {node['key']} {node['host']} already configured")
            else:
                configure_vbond_resolution(
                    node["host"],
                    username,
                    admin_password,
                    vbond_hostname,
                    vbond_ips,
                    vbond_port,
                )
                print(f"[vbond-resolution-updated] {node['key']} {node['host']}")
    else:
        print("[vbond-resolution] skipping explicit vManage update; cloud-init already seeds vbond hostname and IPs")

    if args.skip_cert_install:
        return 0

    for node in nodes:
        host = node["host"]
        key = node["key"]
        role = node["role"]
        node_dir = artifacts_dir / key
        node_dir.mkdir(parents=True, exist_ok=True)

        remote_ca_path = "/home/admin/root-ca.crt"
        remote_csr_path = f"/home/admin/{key}.csr"
        remote_pem_path = f"/home/admin/{key}.pem"
        local_csr_path = node_dir / f"{key}.csr"
        local_pem_path = node_dir / f"{key}.pem"

        print(f"[rootca] {key} {host}")
        wait_for_port(host, 22, 1800, want_open=True)
        scp_to(host, username, admin_password, ca_cert_path, remote_ca_path)
        install_root_ca(host, username, admin_password, remote_ca_path)

        print(f"[csr] {key} {host}")
        generate_csr(host, username, admin_password, organization_name, remote_csr_path)
        scp_from(host, username, admin_password, remote_csr_path, local_csr_path)

        print(f"[sign] {key}")
        sign_csr(ca_cert_path, ca_key_path, local_csr_path, local_pem_path)

        print(f"[cert-install] {key} {host}")
        scp_to(host, username, admin_password, local_pem_path, remote_pem_path)
        install_certificate(host, username, admin_password, remote_pem_path)

        print(f"[verify] {key} {host}")
        verify_controller(role, host, username, admin_password)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - operator-facing script
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)

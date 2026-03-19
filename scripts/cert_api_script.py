#!/usr/bin/env python3
"""Install controller certificates through vManage APIs after cluster formation."""

from __future__ import annotations

import argparse
import json
import re
import secrets
import subprocess
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests

from bootstrap_vmanage_cluster import parse_tfvars_string, terraform_output


MODULE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CA_CERT = MODULE_DIR / "certs" / "controllers" / "root-ca.crt"
DEFAULT_CA_KEY = MODULE_DIR / "certs" / "controllers" / "root-ca.key"
DEFAULT_ARTIFACTS_DIR = MODULE_DIR / "artifacts" / "controller-certs-api"
CONTROLLER_ORDER = (
    "vmanage01",
    "vmanage02",
    "vmanage03",
    "vbond01",
    "vbond02",
    "vsmart01",
    "vsmart02",
)


def log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


def run(cmd: List[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, text=True, capture_output=True, check=check)
    except subprocess.CalledProcessError as exc:  # pragma: no cover - shell failure path
        stdout = exc.stdout.strip()
        stderr = exc.stderr.strip()
        details = [f"Command {exc.cmd!r} failed with exit status {exc.returncode}."]
        if stdout:
            details.append(f"stdout:\n{stdout}")
        if stderr:
            details.append(f"stderr:\n{stderr}")
        raise RuntimeError("\n".join(details)) from exc


def resolve_repo_path(module_dir: Path, raw: str, default: Path) -> Path:
    value = raw.strip()
    if not value:
        return default
    path = Path(value)
    return path if path.is_absolute() else module_dir / path


def ensure_root_ca(ca_cert_path: Path, ca_key_path: Path, organization_name: str) -> None:
    cert_exists = ca_cert_path.exists()
    key_exists = ca_key_path.exists()
    if cert_exists and key_exists:
        return
    if cert_exists != key_exists:
        raise RuntimeError(
            "Controller root CA is incomplete. Expected both "
            f"{ca_cert_path} and {ca_key_path} to exist. "
            "Do not regenerate only one side of a CA after Terraform has already deployed the root certificate."
        )

    ca_cert_path.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
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
    )


def sign_csr(ca_cert_path: Path, ca_key_path: Path, csr_path: Path, pem_path: Path, *, days: int = 3650) -> None:
    pem_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        (tmpdir / "certs").mkdir()
        (tmpdir / "index.txt").write_text("")
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


class VManageApiError(RuntimeError):
    pass


class VManageApiSession:
    def __init__(self, base_url: str, username: str, password: str, timeout: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = False
        requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]
        self.logged_in = False

    def login(self) -> None:
        self.session.cookies.clear()
        response = self.session.get(self.base_url, timeout=self.timeout)
        response.raise_for_status()

        payload = {
            "j_username": self.username,
            "j_password": self.password,
        }
        response = self.session.post(
            f"{self.base_url}/j_security_check",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=self.timeout,
        )
        body = response.text.lower()
        if "j_security_check" in body or "id=\"loginform\"" in body:
            raise VManageApiError(f"Login failed for {self.base_url}")

        token = self.session.get(
            f"{self.base_url}/dataservice/client/token",
            headers={"Accept": "application/json"},
            timeout=self.timeout,
        )
        token.raise_for_status()
        xsrf = token.text.strip().strip('"')
        if xsrf:
            self.session.headers["X-XSRF-TOKEN"] = xsrf
        self.logged_in = True

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        data: Any = None,
        headers: Optional[Dict[str, str]] = None,
        allow_statuses: Iterable[int] = (),
    ) -> Any:
        attempts = 2
        last_error: Optional[Exception] = None
        for _ in range(attempts):
            if not self.logged_in:
                self.login()
            try:
                response = self.session.request(
                    method,
                    f"{self.base_url}{path}",
                    json=json_body,
                    data=data,
                    headers=headers,
                    timeout=self.timeout,
                )
                if response.status_code in allow_statuses:
                    return self._decode_body(response)
                if response.status_code in (401, 403):
                    self.logged_in = False
                    continue
                if response.status_code >= 400:
                    raise VManageApiError(
                        f"HTTP {response.status_code} for {path}: {response.text[:800]}"
                    )
                return self._decode_body(response)
            except (requests.RequestException, VManageApiError) as exc:
                last_error = exc
                if isinstance(exc, VManageApiError) and "HTTP 4" in str(exc):
                    raise
                self.logged_in = False
                time.sleep(5)
        raise VManageApiError(f"Request failed for {path}: {last_error}")

    @staticmethod
    def _decode_body(response: requests.Response) -> Any:
        body = response.text.strip()
        if not body:
            return {}
        try:
            return response.json()
        except ValueError:
            return body


def extract_data_list(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    return []


def choose_management_address(node: Dict[str, Any]) -> str:
    value = node.get("management_public_ip")
    if isinstance(value, str) and value:
        return value
    value = node.get("transport_public_ip")
    if isinstance(value, str) and value:
        return value
    raise RuntimeError(f"No public IP available for {node.get('hostname')}")


def build_config_from_terraform(
    module_dir: Path,
    username: str,
    password: str,
    selected: Optional[set[str]],
    poll_interval_seconds: int,
    ready_timeout_seconds: int,
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
                    "role": role,
                    "system_ip": str(raw.get("system_ip", "")),
                    "transport_ip": str(raw.get("transport_ip", "")),
                    "cluster_ip": str(raw.get("cluster_ip", "")),
                    "management_url": f"https://{choose_management_address(raw)}",
                }
            )
            continue
        if role not in {"vsmart", "vbond"}:
            continue
        if selected and key not in selected:
            continue
        controller_nodes.append(
            {
                "key": str(key),
                "hostname": str(raw.get("hostname", key)),
                "role": role,
                "system_ip": str(raw.get("system_ip", "")),
                "transport_ip": str(raw.get("transport_ip", "")),
                "management_url": f"https://{choose_management_address(raw)}",
            }
        )

    if len(vmanage_nodes) != 3:
        raise RuntimeError(f"Expected 3 vManage nodes, found {len(vmanage_nodes)}")
    if not controller_nodes:
        raise RuntimeError("No vBond or vSmart nodes selected from controller_inventory")

    nodes_by_key = {node["key"]: node for node in [*vmanage_nodes, *controller_nodes]}
    ordered_nodes = [nodes_by_key[key] for key in CONTROLLER_ORDER if key in nodes_by_key]

    return {
        "username": username,
        "password": password,
        "primary_url": vmanage_nodes[0]["management_url"],
        "primary_hostname": vmanage_nodes[0]["hostname"],
        "poll_interval_seconds": poll_interval_seconds,
        "ready_timeout_seconds": ready_timeout_seconds,
        "vmanage_nodes": vmanage_nodes,
        "controller_nodes": controller_nodes,
        "ordered_nodes": ordered_nodes,
    }


def list_registered_controllers(session: VManageApiSession) -> List[Dict[str, Any]]:
    return extract_data_list(session.request("GET", "/dataservice/system/device/controllers"))


def list_controller_certificate_rows(session: VManageApiSession) -> List[Dict[str, Any]]:
    return extract_data_list(session.request("GET", "/dataservice/certificate/data/controller/list"))


def find_row(rows: Iterable[Dict[str, Any]], node: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    hostname = node["hostname"]
    system_ip = node["system_ip"]
    transport_ip = node.get("transport_ip", "")
    cluster_ip = node.get("cluster_ip", "")
    for row in rows:
        if str(row.get("host-name") or row.get("host_name") or "") == hostname:
            return row
        if str(row.get("system-ip") or row.get("system_ip") or "") == system_ip:
            return row
        device_ip = str(row.get("deviceIP") or row.get("device_ip") or "")
        if device_ip and device_ip in {transport_ip, cluster_ip}:
            return row
    return None


def controller_is_registered(rows: Iterable[Dict[str, Any]], node: Dict[str, Any]) -> bool:
    for row in rows:
        device_ip = str(row.get("deviceIP") or row.get("device_ip") or "")
        system_ip = str(row.get("system-ip") or row.get("system_ip") or "")
        host_name = str(row.get("host-name") or row.get("host_name") or "")
        if device_ip == node.get("transport_ip") or system_ip == node["system_ip"] or host_name == node["hostname"]:
            return True
    return False


def wait_for_controller_registration(
    session: VManageApiSession,
    node: Dict[str, Any],
    timeout: int,
    interval: int,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if controller_is_registered(list_registered_controllers(session), node):
            return
        time.sleep(interval)
    raise TimeoutError(f"Timed out waiting for {node['hostname']} to appear in vManage controller inventory")


def add_controller_payload(node: Dict[str, Any], username: str, password: str) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "deviceIP": node["transport_ip"],
        "username": username,
        "password": password,
        "generateCSR": False,
        "personality": node["role"],
    }
    if node["role"] == "vsmart":
        payload["protocol"] = "DTLS"
    return payload


def add_missing_controllers(config: Dict[str, Any], session: VManageApiSession) -> None:
    for role in ("vsmart", "vbond"):
        for node in [item for item in config["controller_nodes"] if item["role"] == role]:
            if controller_is_registered(list_registered_controllers(session), node):
                log(f"{node['hostname']} already exists in vManage")
                continue
            payload = add_controller_payload(node, config["username"], config["password"])
            log(f"Adding {role} {node['hostname']} to {config['primary_hostname']} with generateCSR=false")
            session.request("POST", "/dataservice/system/device", json_body=payload)
            wait_for_controller_registration(
                session,
                node,
                timeout=config["ready_timeout_seconds"],
                interval=config["poll_interval_seconds"],
            )


def controller_cert_installed(row: Dict[str, Any]) -> bool:
    values = [
        str(row.get("certInstallStatus") or ""),
        str(row.get("state") or ""),
    ]
    normalized = [value.lower().replace(" ", "") for value in values if value]
    return any(
        status in {"installed", "certinstalled"} or "certinstalled" in status
        for status in normalized
    )


def wait_for_controller_row(
    session: VManageApiSession,
    node: Dict[str, Any],
    timeout: int,
    interval: int,
) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = find_row(list_controller_certificate_rows(session), node)
        if row:
            return row
        time.sleep(interval)
    raise TimeoutError(f"Timed out waiting for certificate inventory row for {node['hostname']}")


def generate_csr(session: VManageApiSession, device_ip: str) -> None:
    response = session.request(
        "POST",
        "/dataservice/certificate/generate/csr",
        json_body={"deviceIP": device_ip},
        allow_statuses=(400,),
    )
    if isinstance(response, str):
        lowered = response.lower()
        if "already" in lowered and "csr" in lowered:
            return
    if isinstance(response, dict):
        error = str(response.get("error", "")).lower()
        message = str(response.get("message", "")).lower()
        if "already" in error and "csr" in error:
            return
        if "already" in message and "csr" in message:
            return


def row_has_csr(row: Dict[str, Any]) -> bool:
    value = str(row.get("CSRDetail") or row.get("CSR") or "")
    return "BEGIN CERTIFICATE REQUEST" in value


def wait_for_csr(
    session: VManageApiSession,
    node: Dict[str, Any],
    timeout: int,
    interval: int,
) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = wait_for_controller_row(session, node, timeout=interval, interval=max(2, interval // 2))
        if row_has_csr(row):
            return row
        time.sleep(interval)
    raise TimeoutError(f"Timed out waiting for CSR for {node['hostname']}")


def wait_for_action_task(
    session: VManageApiSession,
    task_id: str,
    label: str,
    timeout: int,
    interval: int,
) -> None:
    deadline = time.monotonic() + timeout
    last_payload: Any = None
    while time.monotonic() < deadline:
        try:
            payload = session.request("GET", f"/dataservice/device/action/status/{task_id}")
            last_payload = payload
            if isinstance(payload, dict):
                summary = payload.get("summary")
                if isinstance(summary, dict):
                    status = str(summary.get("status", "")).lower()
                    counts = summary.get("count")
                    if status in {"done", "success"}:
                        if isinstance(counts, dict) and str(counts.get("Failure", "0")) not in {"0", "", "None"}:
                            raise RuntimeError(f"{label} failed: {payload}")
                        return
                    if status in {"fail", "failed", "error"}:
                        raise RuntimeError(f"{label} failed: {payload}")
                data = extract_data_list(payload)
                statuses = [str(item.get("status", "")).lower() for item in data]
                if data and all("success" in status or status in {"done", "completed"} for status in statuses):
                    return
                if any("fail" in status for status in statuses):
                    raise RuntimeError(f"{label} failed: {payload}")
        except Exception:
            pass
        time.sleep(interval)
    raise TimeoutError(f"Timed out waiting for task {task_id} for {label}; last payload={last_payload}")


def install_signed_certificate(
    session: VManageApiSession,
    signed_cert_text: str,
    label: str,
    timeout: int,
    interval: int,
) -> None:
    variants: List[tuple[str, Optional[Dict[str, Any]], Optional[str]]] = [
        ("raw-pem", None, signed_cert_text),
        ("wrapped-certificate", {"certificate": signed_cert_text}, None),
        ("wrapped-signedCert", {"signedCert": signed_cert_text}, None),
    ]
    errors: List[str] = []
    for variant_name, json_body, raw_body in variants:
        try:
            response = session.request(
                "POST",
                "/dataservice/certificate/install/signedCert",
                json_body=json_body,
                data=raw_body,
                headers={"Content-Type": "application/json"},
            )
            if isinstance(response, dict):
                task_id = response.get("id")
                if isinstance(task_id, str) and task_id:
                    wait_for_action_task(session, task_id, f"{label} certificate install", timeout, interval)
                    return
            if isinstance(response, str):
                stripped = response.strip()
                if stripped.startswith("{") and stripped.endswith("}"):
                    parsed = json.loads(stripped)
                    task_id = parsed.get("id")
                    if isinstance(task_id, str) and task_id:
                        wait_for_action_task(session, task_id, f"{label} certificate install", timeout, interval)
                        return
                if "failure" not in stripped.lower():
                    return
            raise RuntimeError(f"Unexpected install response for {label}: {response}")
        except Exception as exc:
            errors.append(f"{variant_name}: {exc}")
    raise RuntimeError(f"Failed to install signed certificate for {label}: {'; '.join(errors)}")


def wait_for_certificate_installed(
    session: VManageApiSession,
    node: Dict[str, Any],
    timeout: int,
    interval: int,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = wait_for_controller_row(session, node, timeout=interval, interval=max(2, interval // 2))
        if controller_cert_installed(row):
            return
        time.sleep(interval)
    raise TimeoutError(f"Timed out waiting for certificate install status for {node['hostname']}")


def trigger_vbond_sync(session: VManageApiSession, timeout: int, interval: int) -> None:
    response = session.request("POST", "/dataservice/certificate/vsmart/list", json_body={})
    if isinstance(response, dict):
        task_id = response.get("id")
        if isinstance(task_id, str) and task_id:
            wait_for_action_task(session, task_id, "vBond certificate sync", timeout, interval)


def get_reachability_rows(session: VManageApiSession, personality: str) -> List[Dict[str, Any]]:
    return extract_data_list(session.request("GET", f"/dataservice/device/reachable?personality={personality}"))


def controller_is_up(row: Dict[str, Any]) -> bool:
    reachability = str(row.get("reachability") or "").lower()
    if reachability != "reachable":
        return False
    expected_raw = row.get("controlConnections")
    up_raw = row.get("controlConnectionsUp")
    if expected_raw is None or up_raw is None:
        return True
    try:
        return int(up_raw) == int(expected_raw)
    except (TypeError, ValueError):
        return str(up_raw) == str(expected_raw)


def wait_for_reachability(
    session: VManageApiSession,
    role: str,
    nodes: List[Dict[str, Any]],
    timeout: int,
    interval: int,
) -> None:
    deadline = time.monotonic() + timeout
    outstanding = {node["hostname"] for node in nodes}
    while time.monotonic() < deadline:
        rows = get_reachability_rows(session, role)
        for row in rows:
            hostname = str(row.get("host-name") or row.get("host_name") or "")
            if hostname in outstanding and controller_is_up(row):
                outstanding.remove(hostname)
        if not outstanding:
            return
        time.sleep(interval)
    raise TimeoutError(f"Timed out waiting for {role} controllers to be UP: {sorted(outstanding)}")


def try_set_controller_signing_enterprise(session: VManageApiSession) -> None:
    payload = {
        "certificateSigning": "enterprise",
        "challenge": "",
        "email": "",
        "firstName": "",
        "lastName": "",
        "retrieveInterval": "60",
        "validityPeriod": "1Y",
    }
    errors: List[str] = []
    for method in ("PUT", "POST"):
        try:
            session.request(
                method,
                "/dataservice/settings/configuration/certificate",
                json_body=payload,
            )
            return
        except Exception as exc:
            errors.append(f"{method}: {exc}")
    raise RuntimeError(f"Unable to set controller certificate signing mode to enterprise: {'; '.join(errors)}")


def upload_enterprise_root_ca(session: VManageApiSession, root_ca_text: str) -> None:
    payload = {"enterpriseRootCA": root_ca_text}
    errors: List[str] = []
    for method in ("PUT", "POST"):
        try:
            session.request(
                method,
                "/dataservice/settings/configuration/certificate/enterpriserootca",
                json_body=payload,
            )
            return
        except Exception as exc:
            errors.append(f"{method}: {exc}")
    raise RuntimeError(f"Unable to upload enterprise root CA to vManage: {'; '.join(errors)}")


def sync_root_ca(session: VManageApiSession) -> None:
    try:
        session.request("GET", "/dataservice/system/device/sync/rootcertchain")
    except Exception as exc:
        log(f"root-cert-chain sync API did not complete cleanly: {exc}")


def parse_selected(values: Optional[str]) -> Optional[set[str]]:
    if not values:
        return None
    return {item.strip() for item in values.split(",") if item.strip()}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Add controllers to vManage, generate controller CSRs through vManage APIs, sign them locally, and install the signed certs back through vManage APIs."
    )
    parser.add_argument("--module-dir", default=str(MODULE_DIR), help="Terraform module directory. Defaults to the repo root.")
    parser.add_argument("--username", default="admin", help="vManage API username. Defaults to admin.")
    parser.add_argument("--password", default=None, help="vManage/controller password. Defaults to admin_password from terraform.tfvars.")
    parser.add_argument("--controllers", default=None, help="Optional subset such as vbond01,vbond02,vsmart01,vsmart02.")
    parser.add_argument("--artifacts-dir", default=str(DEFAULT_ARTIFACTS_DIR), help="Artifact directory for downloaded CSRs and signed certs.")
    parser.add_argument("--ca-cert", default=None, help="Controller root CA certificate path. Defaults to the path from terraform.tfvars.")
    parser.add_argument("--ca-key", default=None, help="Controller root CA private key path. Defaults next to the CA certificate as root-ca.key.")
    parser.add_argument("--poll-interval-seconds", type=int, default=10, help="Polling interval for asynchronous API waits.")
    parser.add_argument("--ready-timeout-seconds", type=int, default=2400, help="Timeout for registration, CSR, certificate install, and reachability waits.")
    args = parser.parse_args()

    module_dir = Path(args.module_dir).resolve()
    artifacts_dir = Path(args.artifacts_dir).resolve()
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    tfvars_path = module_dir / "terraform.tfvars"

    password = args.password or parse_tfvars_string(tfvars_path, "admin_password")
    organization_name = parse_tfvars_string(tfvars_path, "organization_name")
    root_ca_path_str = args.ca_cert or parse_tfvars_string(tfvars_path, "vmanage_root_ca_cert_path")
    ca_cert_path = resolve_repo_path(module_dir, root_ca_path_str, DEFAULT_CA_CERT)
    ca_key_path = resolve_repo_path(module_dir, args.ca_key or str(ca_cert_path.with_name("root-ca.key")), DEFAULT_CA_KEY)
    ensure_root_ca(ca_cert_path, ca_key_path, organization_name)
    root_ca_text = ca_cert_path.read_text().strip()

    config = build_config_from_terraform(
        module_dir=module_dir,
        username=args.username,
        password=password,
        selected=parse_selected(args.controllers),
        poll_interval_seconds=args.poll_interval_seconds,
        ready_timeout_seconds=args.ready_timeout_seconds,
    )
    session = VManageApiSession(config["primary_url"], args.username, password)

    log(f"Using primary vManage {config['primary_hostname']} at {config['primary_url']}")
    log("Setting controller certificate signing mode to enterprise")
    try_set_controller_signing_enterprise(session)

    log("Uploading enterprise root CA into vManage settings")
    upload_enterprise_root_ca(session, root_ca_text)

    log("Triggering root-cert-chain sync from vManage")
    sync_root_ca(session)

    add_missing_controllers(config, session)

    for node in config["ordered_nodes"]:
        row = wait_for_controller_row(session, node, config["ready_timeout_seconds"], config["poll_interval_seconds"])
        if controller_cert_installed(row):
            log(f"{node['hostname']} already shows certInstallStatus installed; skipping")
            continue

        device_ip = str(row.get("deviceIP") or "") or str(node.get("transport_ip") or "") or str(node.get("cluster_ip") or "")
        if not device_ip:
            raise RuntimeError(f"Unable to determine deviceIP for {node['hostname']}")

        if not row_has_csr(row):
            log(f"Generating CSR for {node['hostname']} using deviceIP {device_ip}")
            generate_csr(session, device_ip)
            row = wait_for_csr(session, node, config["ready_timeout_seconds"], config["poll_interval_seconds"])
        else:
            log(f"{node['hostname']} already has a CSR available in vManage")

        csr_text = str(row.get("CSRDetail") or row.get("CSR") or "").strip()
        if "BEGIN CERTIFICATE REQUEST" not in csr_text:
            raise RuntimeError(f"CSR payload for {node['hostname']} is missing the PEM request block")

        csr_path = artifacts_dir / f"{node['key']}.csr"
        pem_path = artifacts_dir / f"{node['key']}.pem"
        csr_path.write_text(csr_text + "\n")
        sign_csr(ca_cert_path, ca_key_path, csr_path, pem_path)

        log(f"Installing signed certificate for {node['hostname']} through vManage API")
        install_signed_certificate(
            session,
            pem_path.read_text(),
            node["hostname"],
            config["ready_timeout_seconds"],
            config["poll_interval_seconds"],
        )
        wait_for_certificate_installed(
            session,
            node,
            config["ready_timeout_seconds"],
            config["poll_interval_seconds"],
        )
        log(f"{node['hostname']} certificate install is complete")

    log("Syncing vSmart certificates to vBond")
    trigger_vbond_sync(session, config["ready_timeout_seconds"], config["poll_interval_seconds"])

    vsmart_nodes = [node for node in config["controller_nodes"] if node["role"] == "vsmart"]
    vbond_nodes = [node for node in config["controller_nodes"] if node["role"] == "vbond"]
    if vsmart_nodes:
        wait_for_reachability(session, "vsmart", vsmart_nodes, config["ready_timeout_seconds"], config["poll_interval_seconds"])
    if vbond_nodes:
        wait_for_reachability(session, "vbond", vbond_nodes, config["ready_timeout_seconds"], config["poll_interval_seconds"])

    log("Controller certificate API flow completed successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

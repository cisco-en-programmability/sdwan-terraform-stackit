#!/usr/bin/env python3
"""Install controller certificates through vManage APIs after cluster formation.

Notes:
- `cisco_pki` is the default mode and expects Cisco Services Registration to be
  completed in the vManage portal with an organization that matches
  `organization_name`.
- The script reads Terraform outputs from the module directory to discover
  controller addresses, certificate ordering, and vManage endpoints.
- Use `--module-dir` if you are running from a copied checkout instead of the
  original repository root.
- The Cisco PKI flow is best-effort per controller so one failing manager does
  not prevent vBond and vSmart enrollment from completing.
"""

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
DEFAULT_CONTROLLER_CERTIFICATE_METHOD = "cisco_pki"
CERTIFICATE_ROW_ENDPOINTS = (
    ("record", "/dataservice/certificate/record"),
    ("controller-list", "/dataservice/certificate/data/controller/list"),
)
CONTROLLER_CERTIFICATE_SETTINGS_ENDPOINTS = (
    "/dataservice/settings/configuration/certificate",
    "/dataservice/settings/configuration/certificate/certificate",
)
SMART_ACCOUNT_SETTINGS_ENDPOINT = "/dataservice/settings/configuration/smartaccountcredentials"
SMART_ACCOUNT_AUTHENTICATE_ENDPOINT = "/dataservice/system/device/smartaccount/authenticate"
SMART_ACCOUNT_SYNC_ENDPOINT = "/dataservice/system/device/smartaccount/sync"
SMART_LICENSING_GET_USER_SETTINGS_ENDPOINT = "/dataservice/smartLicensing/getUserSettings"
SMART_LICENSING_AUTHENTICATE_ENDPOINT = "/dataservice/smartLicensing/authenticate"
SMART_LICENSING_FETCH_ACCOUNTS_ENDPOINT = "/dataservice/smartLicensing/fetchAccounts?mode=online"
CISCO_SERVICES_ENDPOINTS = (
    "/dataservice/settings/configuration/ciscoServices",
    "/dataservice/settings/configuration/ciscoservices",
)
PNP_CONNECT_SYNC_ENDPOINT = "/dataservice/settings/configuration/pnpConnectSync"
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


def sleep_with_log(seconds: int, reason: str) -> None:
    log(f"sleeping {seconds}s: {reason}")
    time.sleep(seconds)


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


def parse_tfvars_string_or_default(tfvars_path: Path, key: str, default: str = "") -> str:
    try:
        return parse_tfvars_string(tfvars_path, key)
    except RuntimeError:
        return default


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
        attempts = 6
        last_error: Optional[Exception] = None
        for _ in range(attempts):
            try:
                if not self.logged_in:
                    self.login()
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
                if isinstance(exc, VManageApiError) and any(code in str(exc) for code in ("HTTP 400", "HTTP 404")):
                    raise
                self.logged_in = False
                sleep_with_log(10, f"retrying vManage API request {method} {path}")
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


def extract_first_row(payload: Any) -> Dict[str, Any]:
    rows = extract_data_list(payload)
    if rows:
        return rows[0]
    if isinstance(payload, dict):
        return payload
    return {}


def normalize_pem_text(value: str) -> str:
    return "\n".join(line.strip() for line in value.strip().splitlines() if line.strip())


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
                "management_ip": str(raw.get("management_ip", "")),
                "management_public_ip": str(raw.get("management_public_ip", "")),
                "transport_ip": str(raw.get("transport_ip", "")),
                "transport_public_ip": str(raw.get("transport_public_ip", "")),
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


def log_cisco_pki_prereqs(config: Dict[str, Any], organization_name: str, vbond_hostname: str) -> None:
    log("Cisco PKI prerequisites for this deployment:")
    log(f"  Smart Account organization must match organization_name={organization_name}")
    log(f"  vBond DNS name: {vbond_hostname}")

    vbond_nodes = [node for node in config["controller_nodes"] if node["role"] == "vbond"]
    if not vbond_nodes:
        log("  No vBond nodes were selected in this run")
        return

    for node in vbond_nodes:
        parts = [
            f"{node['hostname']}",
            f"transport_ip={node.get('transport_ip') or 'N/A'}",
            f"transport_public_ip={node.get('transport_public_ip') or 'N/A'}",
            f"management_ip={node.get('management_ip') or 'N/A'}",
            f"management_public_ip={node.get('management_public_ip') or 'N/A'}",
        ]
        log("  " + " ".join(parts))


def log_vbond_registration_details(config: Dict[str, Any], vbond_hostname: str) -> None:
    log("Use the following vBond details in the Cisco portal controller profile if prompted:")
    log(f"  vBond DNS/FQDN: {vbond_hostname}")

    vbond_nodes = [node for node in config["controller_nodes"] if node["role"] == "vbond"]
    if not vbond_nodes:
        log("  No vBond nodes were selected in this run")
        return

    for node in vbond_nodes:
        log(f"  vBond hostname: {node['hostname']}")
        log(f"    transport_public_ip: {node.get('transport_public_ip') or 'N/A'}")
        log(f"    management_public_ip: {node.get('management_public_ip') or 'N/A'}")
        log(f"    transport_ip: {node.get('transport_ip') or 'N/A'}")
        log(f"    management_ip: {node.get('management_ip') or 'N/A'}")


def confirm_certificate_enrollment(controller_certificate_method: str, auto_approve: bool) -> None:
    if auto_approve:
        return
    log(
        "Certificate enrollment is about to begin. Ensure the cluster is stable before proceeding."
    )
    if controller_certificate_method == "cisco_pki":
        log(
            "For Cisco PKI, vManage will submit controller CSRs and then push updated controller lists and serial lists across the cluster."
        )
    else:
        log(
            "For enterprise_local, vManage will install locally signed controller certificates and then push updated controller lists and serial lists across the cluster."
        )
    confirmation = input("Type yes to continue with controller certificate enrollment: ").strip().lower()
    if confirmation != "yes":
        raise RuntimeError("Controller certificate enrollment was not confirmed. Aborting before CSR generation.")


def list_registered_controllers(session: VManageApiSession) -> List[Dict[str, Any]]:
    return extract_data_list(session.request("GET", "/dataservice/system/device/controllers"))


def list_controller_certificate_rows(session: VManageApiSession) -> List[tuple[str, List[Dict[str, Any]]]]:
    rows_by_endpoint: List[tuple[str, List[Dict[str, Any]]]] = []
    errors: List[str] = []
    for label, path in CERTIFICATE_ROW_ENDPOINTS:
        try:
            rows_by_endpoint.append((label, extract_data_list(session.request("GET", path))))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{label}: {exc}")
    if rows_by_endpoint:
        return rows_by_endpoint
    raise RuntimeError(f"Unable to query any controller certificate inventory endpoint: {'; '.join(errors)}")


def find_row(rows: Iterable[Dict[str, Any]], node: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    hostname = node["hostname"]
    system_ip = node["system_ip"]
    management_public_ip = node.get("management_public_ip", "")
    management_ip = node.get("management_ip", "")
    transport_public_ip = node.get("transport_public_ip", "")
    transport_ip = node.get("transport_ip", "")
    cluster_ip = node.get("cluster_ip", "")
    for row in rows:
        if str(row.get("host-name") or row.get("host_name") or "") == hostname:
            return row
        if str(row.get("system-ip") or row.get("system_ip") or "") == system_ip:
            return row
        device_ip = str(row.get("deviceIP") or row.get("device_ip") or "")
        if device_ip and device_ip in {
            management_public_ip,
            management_ip,
            transport_public_ip,
            transport_ip,
            cluster_ip,
        }:
            return row
    return None


def controller_is_registered(rows: Iterable[Dict[str, Any]], node: Dict[str, Any]) -> bool:
    for row in rows:
        device_ip = str(row.get("deviceIP") or row.get("device_ip") or "")
        system_ip = str(row.get("system-ip") or row.get("system_ip") or "")
        host_name = str(row.get("host-name") or row.get("host_name") or "")
        if (
            device_ip
            in {
                node.get("management_public_ip"),
                node.get("management_ip"),
                node.get("transport_public_ip"),
                node.get("transport_ip"),
            }
            or system_ip == node["system_ip"]
            or host_name == node["hostname"]
        ):
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
        sleep_with_log(interval, f"waiting for {node['hostname']} to appear in vManage controller inventory")
    raise TimeoutError(f"Timed out waiting for {node['hostname']} to appear in vManage controller inventory")


def choose_controller_add_device_ip(node: Dict[str, Any]) -> str:
    for field in ("management_public_ip", "management_ip", "transport_public_ip", "transport_ip"):
        value = str(node.get(field) or "").strip()
        if value:
            return value
    raise RuntimeError(f"No usable controller deviceIP found for {node['hostname']}")


def add_controller_payload(node: Dict[str, Any], username: str, password: str) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "deviceIP": choose_controller_add_device_ip(node),
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
            deadline = time.monotonic() + config["ready_timeout_seconds"]
            attempt = 0
            last_error: Optional[Exception] = None
            while time.monotonic() < deadline:
                attempt += 1
                try:
                    log(
                        f"Adding {role} {node['hostname']} to {config['primary_hostname']} "
                        f"with generateCSR=false (attempt {attempt})"
                    )
                    session.request("POST", "/dataservice/system/device", json_body=payload)
                    wait_for_controller_registration(
                        session,
                        node,
                        timeout=config["ready_timeout_seconds"],
                        interval=config["poll_interval_seconds"],
                    )
                    last_error = None
                    break
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    message = str(exc)
                    if "SYSTD0001" not in message and "Failed to authenticate" not in message:
                        raise
                    log(
                        f"{node['hostname']} add attempt {attempt} returned an authentication error; "
                        f"retrying after {config['poll_interval_seconds']}s"
                    )
                    sleep_with_log(
                        config["poll_interval_seconds"],
                        f"retrying add-controller request for {node['hostname']}",
                    )
            if last_error is not None:
                raise RuntimeError(f"Unable to add {node['hostname']} to vManage: {last_error}")


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


def controller_cert_failed(row: Dict[str, Any]) -> bool:
    state = str(row.get("state") or "").strip().lower()
    install_status = str(row.get("certInstallStatus") or "").strip().lower()
    error_detail = str(row.get("errorDetail") or "").strip()
    if error_detail and error_detail.upper() != "N/A":
        return True
    if state in {"error", "failed", "fail"}:
        return True
    if install_status in {"error", "failed", "fail"}:
        return True
    return False


def extract_csr_text(row: Dict[str, Any]) -> str:
    for field in ("deviceCSR", "CSRDetail", "CSR"):
        value = str(row.get(field) or "").strip()
        if not value or value.upper() == "N/A":
            continue
        if "BEGIN CERTIFICATE REQUEST" in value:
            return value
    return ""


def select_best_row(rows: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    best_row: Optional[Dict[str, Any]] = None
    best_score = -1
    for row in rows:
        score = 0
        if extract_csr_text(row):
            score += 100
        if controller_cert_installed(row):
            score += 10
        if str(row.get("state") or "").strip():
            score += 5
        if str(row.get("certInstallStatus") or "").strip():
            score += 5
        if str(row.get("__source") or "") == "record":
            score += 2
        if score > best_score:
            best_row = row
            best_score = score
    return best_row


def choose_device_ip_for_csr(node: Dict[str, Any], row: Dict[str, Any]) -> str:
    if node["role"] == "vmanage":
        if node.get("key") != "vmanage01":
            cluster_ip = str(node.get("cluster_ip") or "").strip()
            if cluster_ip:
                return cluster_ip
        for value in (
            str(row.get("deviceIP") or "").strip(),
            str(node.get("system_ip") or "").strip(),
            str(node.get("cluster_ip") or "").strip(),
        ):
            if value:
                return value
    for value in (
        str(row.get("deviceIP") or "").strip(),
        str(node.get("transport_ip") or "").strip(),
        str(node.get("system_ip") or "").strip(),
    ):
        if value:
            return value
    raise RuntimeError(f"Unable to determine deviceIP for {node['hostname']}")


def wait_for_controller_row(
    session: VManageApiSession,
    node: Dict[str, Any],
    timeout: int,
    interval: int,
) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matches: List[Dict[str, Any]] = []
        for source, rows in list_controller_certificate_rows(session):
            row = find_row(rows, node)
            if row:
                annotated = dict(row)
                annotated["__source"] = source
                matches.append(annotated)
        row = select_best_row(matches)
        if row:
            return row
        sleep_with_log(interval, f"waiting for a certificate inventory row for {node['hostname']}")
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
        if not error and (
            "data" in response
            or "header" in response
            or "id" in response
            or "uuid" in response
            or _payload_is_success(response)
        ):
            return
        details = response.get("details")
        raise RuntimeError(
            f"CSR generation request failed for deviceIP {device_ip}: "
            f"message={response.get('message')!r} error={response.get('error')!r} details={details!r}"
        )
    if isinstance(response, str):
        raise RuntimeError(f"CSR generation request failed for deviceIP {device_ip}: {response}")


def csr_session_for_node(
    primary_session: VManageApiSession,
    config: Dict[str, Any],
    node: Dict[str, Any],
) -> VManageApiSession:
    if node.get("role") != "vmanage":
        return primary_session
    management_url = str(node.get("management_url") or "").strip()
    if not management_url or management_url == config["primary_url"]:
        return primary_session
    return VManageApiSession(management_url, config["username"], config["password"])


def row_has_csr(row: Dict[str, Any]) -> bool:
    return bool(extract_csr_text(row))


def controller_csr_requested(row: Dict[str, Any]) -> bool:
    if row_has_csr(row):
        return True
    state = str(row.get("state") or "").strip().lower()
    if state in {"csr generated", "csr requested"}:
        return True
    if any(token in state for token in ("csr", "certificate", "request", "pending", "install")):
        return True
    request_token = str(row.get("requestTokenID") or "").strip()
    if request_token and request_token.upper() != "N/A" and state:
        return True
    activity = row.get("activity")
    if isinstance(activity, list):
        for item in activity:
            lowered = str(item).lower()
            if any(token in lowered for token in ("csr", "certificate", "request", "install")):
                return True
    return False


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
        sleep_with_log(interval, f"waiting for CSR text for {node['hostname']}")
    raise TimeoutError(f"Timed out waiting for CSR for {node['hostname']}")


def wait_for_csr_request_submitted(
    session: VManageApiSession,
    node: Dict[str, Any],
    timeout: int,
    interval: int,
) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = wait_for_controller_row(session, node, timeout=interval, interval=max(2, interval // 2))
        if controller_cert_failed(row):
            raise RuntimeError(f"CSR generation failed for {node['hostname']}: {row}")
        if controller_csr_requested(row):
            return row
        sleep_with_log(interval, f"waiting for CSR submission state for {node['hostname']}")
    raise TimeoutError(f"Timed out waiting for CSR submission for {node['hostname']}")


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
        sleep_with_log(interval, f"waiting for task {task_id} for {label}")
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
        if controller_cert_failed(row):
            raise RuntimeError(f"Certificate install failed for {node['hostname']}: {row}")
        if controller_cert_installed(row):
            return
        sleep_with_log(interval, f"waiting for certificate install status for {node['hostname']}")
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


def wait_for_vmanage_relogin(
    base_url: str,
    username: str,
    password: str,
    timeout: int,
    interval: int,
) -> VManageApiSession:
    deadline = time.monotonic() + timeout
    last_error: Optional[Exception] = None
    while time.monotonic() < deadline:
        session = VManageApiSession(base_url, username, password)
        try:
            session.login()
            return session
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            sleep_with_log(interval, f"retrying login to {base_url}")
    raise TimeoutError(f"Timed out waiting to re-login to {base_url}: {last_error}")


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
        sleep_with_log(interval, f"waiting for {role} controllers to come UP: {sorted(outstanding)}")
    raise TimeoutError(f"Timed out waiting for {role} controllers to be UP: {sorted(outstanding)}")


def update_controller_signing(session: VManageApiSession, payload: Dict[str, Any], label: str) -> None:
    errors: List[str] = []
    for path in CONTROLLER_CERTIFICATE_SETTINGS_ENDPOINTS:
        for method in ("PUT", "POST"):
            try:
                session.request(
                    method,
                    path,
                    json_body=payload,
                )
                return
            except Exception as exc:
                errors.append(f"{method} {path}: {exc}")
    raise RuntimeError(f"Unable to update controller certificate settings for {label}: {'; '.join(errors)}")


def try_set_controller_signing_enterprise(session: VManageApiSession) -> None:
    update_controller_signing(
        session,
        {
            "certificateSigning": "enterprise",
        },
        "enterprise_local",
    )


def try_set_controller_signing_cisco(session: VManageApiSession) -> None:
    update_controller_signing(
        session,
        {
            "certificateSigning": "cisco",
            "validityPeriod": "5Y",
            "retrieveInterval": "1",
        },
        "cisco_pki",
    )


def get_controller_certificate_settings(session: VManageApiSession) -> Dict[str, Any]:
    errors: List[str] = []
    for path in CONTROLLER_CERTIFICATE_SETTINGS_ENDPOINTS:
        try:
            row = extract_first_row(session.request("GET", path))
            if row:
                return row
        except Exception as exc:
            errors.append(f"{path}: {exc}")
    if errors:
        raise RuntimeError(f"Unable to read controller certificate settings: {'; '.join(errors)}")
    return {}


def wait_for_controller_signing_mode(
    session: VManageApiSession,
    expected: str,
    timeout: int,
    interval: int,
) -> None:
    deadline = time.monotonic() + timeout
    expected_normalized = expected.strip().lower()
    while time.monotonic() < deadline:
        row = get_controller_certificate_settings(session)
        signing = str(row.get("certificateSigning") or "").strip().lower()
        if signing == expected_normalized:
            return
        sleep_with_log(interval, f"waiting for controller signing mode to become {expected}")
    raise TimeoutError(f"Timed out waiting for controller certificate signing mode {expected}")


def upload_enterprise_root_ca(session: VManageApiSession, root_ca_text: str) -> None:
    payload = {"enterpriseRootCA": root_ca_text}
    errors: List[str] = []
    original_timeout = session.timeout
    session.timeout = max(session.timeout, 180)
    try:
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
    finally:
        session.timeout = original_timeout
    raise RuntimeError(f"Unable to upload enterprise root CA to vManage: {'; '.join(errors)}")


def sync_root_ca(session: VManageApiSession) -> None:
    try:
        session.request("GET", "/dataservice/system/device/sync/rootcertchain")
    except Exception as exc:
        log(f"root-cert-chain sync API did not complete cleanly: {exc}")


def get_enterprise_root_ca(session: VManageApiSession) -> str:
    payload = session.request("GET", "/dataservice/settings/configuration/certificate/enterpriserootca")
    rows = extract_data_list(payload)
    if not rows:
        return ""
    return str(rows[0].get("enterpriseRootCA") or "").strip()


def prompt_manual_cisco_services_registration(config: Dict[str, Any], organization_name: str, vbond_hostname: str) -> None:
    log("Manual Cisco Services Registration is required before Cisco PKI certificate enrollment can continue.")
    log(f"Open vManage portal: {config['primary_url']}")
    log(f"Login username: {config['username']}")
    log(f"Login password: {config['password']}")
    log(f"Organization name must match: {organization_name}")
    log_vbond_registration_details(config, vbond_hostname)
    log("In the vManage portal, go to: Settings > Cisco Services Registration")
    log("Select Plug-and-Play, choose Register Services, complete the activation-code flow, enter the Smart Account username and password, and enable it for Plug-and-Play.")
    confirmation = input("Type yes after Cisco Services Registration is completed successfully in the vManage portal: ").strip().lower()
    if confirmation != "yes":
        raise RuntimeError("Cisco Services Registration was not confirmed. Aborting before CSR generation.")


def get_smart_account_credentials(session: VManageApiSession) -> Dict[str, Any]:
    return extract_first_row(session.request("GET", SMART_ACCOUNT_SETTINGS_ENDPOINT))


def get_smart_licensing_user_settings(session: VManageApiSession) -> Dict[str, Any]:
    return extract_first_row(session.request("GET", SMART_LICENSING_GET_USER_SETTINGS_ENDPOINT))


def smart_licensing_credentials_present(row: Dict[str, Any]) -> bool:
    value = row.get("isPresentCredentials")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() == "true"


def authenticate_smart_licensing(session: VManageApiSession, username: str, password: str) -> None:
    response = session.request(
        "POST",
        SMART_LICENSING_AUTHENTICATE_ENDPOINT,
        json_body={"username": username, "password": password, "mode": "online"},
    )
    if not _payload_is_success(response):
        raise RuntimeError(f"Unexpected Smart Licensing authenticate response: {response}")


def fetch_smart_licensing_accounts(session: VManageApiSession) -> Any:
    return session.request("GET", SMART_LICENSING_FETCH_ACCOUNTS_ENDPOINT)


def sync_smart_account_registration(session: VManageApiSession, username: str, password: str, validity_string: str = "invalid") -> None:
    response = session.request(
        "POST",
        SMART_ACCOUNT_SYNC_ENDPOINT,
        json_body={"username": username, "password": password, "validity_string": validity_string},
    )
    if isinstance(response, dict):
        task_id = response.get("id")
        if isinstance(task_id, str) and task_id:
            return
    if not _payload_is_success(response):
        raise RuntimeError(f"Unexpected Smart Account sync response: {response}")


def wait_for_smart_licensing_ready(
    session: VManageApiSession,
    username: Optional[str],
    password: Optional[str],
    timeout: int,
    interval: int,
) -> None:
    deadline = time.monotonic() + timeout
    last_error: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            if username and password is not None:
                authenticate_smart_licensing(session, username, password)
            settings = get_smart_licensing_user_settings(session)
            if not smart_licensing_credentials_present(settings):
                raise RuntimeError(f"Smart Licensing credentials are not present yet: {settings}")
            fetch_smart_licensing_accounts(session)
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            sleep_with_log(interval, "waiting for Smart Licensing readiness")
    raise TimeoutError(f"Timed out waiting for Smart Licensing readiness: {last_error}")


def wait_for_manual_cisco_services_registration(
    session: VManageApiSession,
    timeout: int,
    interval: int,
) -> None:
    deadline = time.monotonic() + timeout
    last_error: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            account_row = get_smart_account_credentials(session)
            pnp_row = get_pnp_connect_sync(session)
            cisco_services = list_cisco_services(session)
            if not plug_and_play_registered(cisco_services):
                raise RuntimeError(
                    "Cisco Services Registration is incomplete: Plug-and-Play is not registered yet "
                    f"(ciscoServices={cisco_services})"
                )
            # On some builds the dedicated Smart Account and PnP settings endpoints
            # stay empty even after the portal workflow completes. Treat the
            # Cisco Services Plug-and-Play registration row as the authoritative
            # signal once it is present, and only use the older endpoints as
            # supporting evidence when they populate.
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            sleep_with_log(interval, "waiting for manual Cisco Services Registration to become active")
    raise TimeoutError(f"Timed out waiting for manual Cisco Services Registration to become active: {last_error}")


def configure_smart_account_credentials(session: VManageApiSession, username: str, password: str) -> None:
    payload = {
        "username": username,
        "password": password,
    }
    errors: List[str] = []
    for method in ("PUT", "POST"):
        try:
            session.request(method, SMART_ACCOUNT_SETTINGS_ENDPOINT, json_body=payload)
            return
        except Exception as exc:
            errors.append(f"{method}: {exc}")
    raise RuntimeError(f"Unable to configure Smart Account credentials: {'; '.join(errors)}")


def smart_account_username_matches(row: Dict[str, Any], username: str) -> bool:
    normalized = username.strip().lower()
    for field in ("username", "userName"):
        value = str(row.get(field) or "").strip().lower()
        if value and value == normalized:
            return True
    return False


def get_pnp_connect_sync(session: VManageApiSession) -> Dict[str, Any]:
    return extract_first_row(session.request("GET", PNP_CONNECT_SYNC_ENDPOINT))


def pnp_connect_sync_enabled(row: Dict[str, Any]) -> bool:
    return str(row.get("mode") or "").strip().lower() == "on"


def list_cisco_services(session: VManageApiSession) -> List[Dict[str, Any]]:
    errors: List[str] = []
    for path in CISCO_SERVICES_ENDPOINTS:
        try:
            return extract_data_list(session.request("GET", path))
        except Exception as exc:
            errors.append(f"{path}: {exc}")
    raise RuntimeError(f"Unable to query Cisco Services Registration status: {'; '.join(errors)}")


def plug_and_play_registered(rows: Iterable[Dict[str, Any]]) -> bool:
    for row in rows:
        service_name = str(row.get("service_name") or row.get("serviceName") or "").strip().lower()
        if service_name != "plug-n-play":
            continue
        user_id = str(row.get("user_id") or row.get("userId") or "").strip()
        updated_by = str(row.get("updated_by") or row.get("updatedBy") or "").strip()
        if user_id and user_id != "—" and updated_by and updated_by != "—":
            return True
    return False


def set_pnp_connect_sync(session: VManageApiSession, enabled: bool) -> None:
    payload = {"mode": "on" if enabled else "off"}
    errors: List[str] = []
    for method in ("PUT", "POST"):
        try:
            session.request(method, PNP_CONNECT_SYNC_ENDPOINT, json_body=payload)
            return
        except Exception as exc:
            errors.append(f"{method}: {exc}")
    raise RuntimeError(f"Unable to set PnP Connect Sync to {payload['mode']}: {'; '.join(errors)}")


def wait_for_pnp_connect_sync(
    session: VManageApiSession,
    enabled: bool,
    timeout: int,
    interval: int,
) -> None:
    deadline = time.monotonic() + timeout
    last_row: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        row = get_pnp_connect_sync(session)
        last_row = row
        if pnp_connect_sync_enabled(row) == enabled:
            return
        sleep_with_log(interval, f"waiting for PnP Connect Sync to turn {'on' if enabled else 'off'}")
    state = "on" if enabled else "off"
    raise TimeoutError(f"Timed out waiting for PnP Connect Sync {state}: {last_row}")


def _payload_is_success(payload: Any) -> bool:
    if payload in ({}, [], None):
        return True
    if isinstance(payload, str):
        lowered = payload.lower()
        return not any(token in lowered for token in ("error", "failed", "invalid", "unauthorized"))
    if isinstance(payload, dict):
        for field in ("error", "errorDetail", "details"):
            value = str(payload.get(field) or "").strip()
            if value and any(token in value.lower() for token in ("error", "failed", "invalid", "unauthorized")):
                return False
        summary = json.dumps(payload).lower()
        return not any(token in summary for token in ("authentication failed", "invalid credential", "\"error\""))
    return True


def authenticate_smart_account(
    session: VManageApiSession,
    username: str,
    password: str,
    timeout: int,
    interval: int,
) -> None:
    variants: List[tuple[str, Optional[Dict[str, Any]]]] = [
        ("empty-body", {}),
        ("credential-body", {"username": username, "password": password}),
    ]
    errors: List[str] = []
    for variant_name, payload in variants:
        try:
            response = session.request("POST", SMART_ACCOUNT_AUTHENTICATE_ENDPOINT, json_body=payload)
            if isinstance(response, dict):
                task_id = response.get("id")
                if isinstance(task_id, str) and task_id:
                    wait_for_action_task(session, task_id, "Smart Account authenticate", timeout, interval)
                    return
            if _payload_is_success(response):
                return
            raise RuntimeError(f"Unexpected Smart Account authenticate response: {response}")
        except Exception as exc:
            errors.append(f"{variant_name}: {exc}")
    raise RuntimeError(f"Unable to validate Smart Account credentials: {'; '.join(errors)}")


def wait_for_smart_account_validation(
    session: VManageApiSession,
    username: str,
    password: str,
    timeout: int,
    interval: int,
) -> None:
    deadline = time.monotonic() + timeout
    last_error: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            row = get_smart_account_credentials(session)
            if not smart_account_username_matches(row, username):
                raise RuntimeError(f"Smart Account username is not active yet: {row}")
            authenticate_smart_account(session, username, password, timeout=interval * 6, interval=max(2, interval // 2))
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            sleep_with_log(interval, "waiting for Smart Account validation")
    raise TimeoutError(f"Timed out waiting for Smart Account validation: {last_error}")


def parse_selected(values: Optional[str]) -> Optional[set[str]]:
    if not values:
        return None
    return {item.strip() for item in values.split(",") if item.strip()}


def run_enterprise_local_flow(
    session: VManageApiSession,
    config: Dict[str, Any],
    ca_cert_path: Path,
    ca_key_path: Path,
    root_ca_text: str,
    artifacts_dir: Path,
) -> None:
    log("Setting controller certificate signing mode to enterprise")
    try_set_controller_signing_enterprise(session)
    log("Waiting for vManage API login to recover after signing-mode change")
    session = wait_for_vmanage_relogin(
        config["primary_url"],
        config["username"],
        config["password"],
        config["ready_timeout_seconds"],
        config["poll_interval_seconds"],
    )
    wait_for_controller_signing_mode(
        session,
        "enterprise",
        config["ready_timeout_seconds"],
        config["poll_interval_seconds"],
    )

    existing_root_ca = get_enterprise_root_ca(session)
    if normalize_pem_text(existing_root_ca) == normalize_pem_text(root_ca_text):
        log("Enterprise root CA already matches the local controller CA; skipping upload")
    else:
        log("Uploading enterprise root CA into vManage settings")
        upload_enterprise_root_ca(session, root_ca_text)
        log("Refreshing vManage API session after enterprise root CA upload")
        session = wait_for_vmanage_relogin(
            config["primary_url"],
            config["username"],
            config["password"],
            config["ready_timeout_seconds"],
            config["poll_interval_seconds"],
        )

    log("Triggering root-cert-chain sync from vManage")
    sync_root_ca(session)
    log("Refreshing vManage API session after root-cert-chain sync")
    session = wait_for_vmanage_relogin(
        config["primary_url"],
        config["username"],
        config["password"],
        config["ready_timeout_seconds"],
        config["poll_interval_seconds"],
    )

    add_missing_controllers(config, session)

    for node in config["ordered_nodes"]:
        row = wait_for_controller_row(session, node, config["ready_timeout_seconds"], config["poll_interval_seconds"])
        if controller_cert_installed(row):
            log(f"{node['hostname']} already shows certInstallStatus installed; skipping")
            continue

        device_ip = choose_device_ip_for_csr(node, row)
        node_csr_session = csr_session_for_node(session, config, node)

        if not row_has_csr(row):
            log(f"Generating CSR for {node['hostname']} using deviceIP {device_ip}")
            generate_csr(node_csr_session, device_ip)
            row = wait_for_csr(session, node, config["ready_timeout_seconds"], config["poll_interval_seconds"])
        else:
            log(f"{node['hostname']} already has a CSR available in vManage")

        csr_text = extract_csr_text(row)
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


def run_cisco_pki_flow(
    session: VManageApiSession,
    config: Dict[str, Any],
    smart_account_username: Optional[str],
    smart_account_password: Optional[str],
    organization_name: str,
    vbond_hostname: str,
    smart_account_preconfigured: bool = False,
) -> None:
    log(
        "Setting controller certificate signing mode to Cisco PKI. "
        f"Smart Account organization must match organization_name={organization_name}"
    )
    try_set_controller_signing_cisco(session)
    log("Waiting for vManage API login to recover after signing-mode change")
    session = wait_for_vmanage_relogin(
        config["primary_url"],
        config["username"],
        config["password"],
        config["ready_timeout_seconds"],
        config["poll_interval_seconds"],
    )
    wait_for_controller_signing_mode(
        session,
        "cisco",
        config["ready_timeout_seconds"],
        config["poll_interval_seconds"],
    )

    if smart_account_preconfigured:
        log("Cisco Services Registration is marked as preconfigured; validating Smart Account, Plug-and-Play, and Cisco services state")
    else:
        prompt_manual_cisco_services_registration(config, organization_name, vbond_hostname)
    log("Validating Cisco Services Registration state via vManage APIs")
    wait_for_manual_cisco_services_registration(
        session,
        config["ready_timeout_seconds"],
        config["poll_interval_seconds"],
    )

    add_missing_controllers(config, session)

    installed_keys: set[str] = set()
    pending_nodes: List[Dict[str, Any]] = []
    failures: Dict[str, str] = {}

    for node in config["ordered_nodes"]:
        row = wait_for_controller_row(session, node, config["ready_timeout_seconds"], config["poll_interval_seconds"])
        if controller_cert_installed(row):
            log(f"{node['hostname']} already shows certInstallStatus installed; skipping")
            installed_keys.add(node["key"])
            continue

        device_ip = choose_device_ip_for_csr(node, row)
        node_csr_session = csr_session_for_node(session, config, node)
        try:
            log(f"Ensuring Cisco PKI CSR is triggered for {node['hostname']} using deviceIP {device_ip}")
            generate_csr(node_csr_session, device_ip)
            pending_nodes.append(node)
        except Exception as exc:  # noqa: BLE001
            failures[node["hostname"]] = f"CSR trigger failed: {exc}"
            log(f"{node['hostname']} CSR trigger failed; continuing with other controllers: {exc}")

    if pending_nodes:
        log("Waiting for Cisco PKI certificate install across all pending controllers")

    observed_csr_hosts: set[str] = set()
    deadline = time.monotonic() + config["ready_timeout_seconds"]
    while pending_nodes and time.monotonic() < deadline:
        next_pending: List[Dict[str, Any]] = []
        for node in pending_nodes:
            row = wait_for_controller_row(
                session,
                node,
                timeout=config["poll_interval_seconds"],
                interval=max(2, config["poll_interval_seconds"] // 2),
            )
            if controller_cert_failed(row):
                failures[node["hostname"]] = f"Certificate install failed: {row}"
                log(f"{node['hostname']} certificate install failed; continuing with remaining controllers")
                continue
            if controller_csr_requested(row) and node["hostname"] not in observed_csr_hosts:
                observed_csr_hosts.add(node["hostname"])
                log(f"{node['hostname']} CSR request is visible in certificate inventory")
            if controller_cert_installed(row):
                installed_keys.add(node["key"])
                log(f"{node['hostname']} certificate install is complete")
                continue
            next_pending.append(node)
        pending_nodes = next_pending
        if pending_nodes:
            sleep_with_log(
                config["poll_interval_seconds"],
                "waiting for Cisco PKI certificate install on "
                + ", ".join(node["hostname"] for node in pending_nodes),
            )

    for node in pending_nodes:
        failures[node["hostname"]] = "Timed out waiting for Cisco PKI certificate install"
        log(f"{node['hostname']} did not finish certificate install before timeout")

    log("Syncing vSmart certificates to vBond")
    trigger_vbond_sync(session, config["ready_timeout_seconds"], config["poll_interval_seconds"])

    vsmart_nodes = [node for node in config["controller_nodes"] if node["role"] == "vsmart" and node["key"] in installed_keys]
    vbond_nodes = [node for node in config["controller_nodes"] if node["role"] == "vbond" and node["key"] in installed_keys]
    if vsmart_nodes:
        wait_for_reachability(session, "vsmart", vsmart_nodes, config["ready_timeout_seconds"], config["poll_interval_seconds"])
    if vbond_nodes:
        wait_for_reachability(session, "vbond", vbond_nodes, config["ready_timeout_seconds"], config["poll_interval_seconds"])

    cleared_hosts: List[str] = []
    for node in config["ordered_nodes"]:
        if node["hostname"] not in failures:
            continue
        try:
            row = wait_for_controller_row(
                session,
                node,
                timeout=config["poll_interval_seconds"],
                interval=max(2, config["poll_interval_seconds"] // 2),
            )
        except Exception:
            continue
        if controller_cert_installed(row):
            installed_keys.add(node["key"])
            cleared_hosts.append(node["hostname"])
    for hostname in cleared_hosts:
        failures.pop(hostname, None)

    if failures:
        failure_text = "; ".join(f"{host}: {reason}" for host, reason in sorted(failures.items()))
        raise RuntimeError(f"Cisco PKI completed with controller failures: {failure_text}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install controller certificates through vManage APIs after cluster formation using either the default Cisco PKI flow or the enterprise-local fallback flow."
    )
    parser.add_argument("--module-dir", default=str(MODULE_DIR), help="Terraform module directory. Defaults to the repo root.")
    parser.add_argument("--username", default="admin", help="vManage API username. Defaults to admin.")
    parser.add_argument("--password", default=None, help="vManage/controller password. Defaults to admin_password from terraform.tfvars.")
    parser.add_argument(
        "--controller-certificate-method",
        choices=("cisco_pki", "enterprise_local"),
        default=None,
        help="Certificate flow override. Defaults to controller_certificate_method from terraform.tfvars, else cisco_pki.",
    )
    parser.add_argument("--controllers", default=None, help="Optional subset such as vbond01,vbond02,vsmart01,vsmart02.")
    parser.add_argument("--yes", action="store_true", help="Skip the certificate enrollment confirmation prompt.")
    parser.add_argument("--artifacts-dir", default=str(DEFAULT_ARTIFACTS_DIR), help="Artifact directory for downloaded CSRs and signed certs.")
    parser.add_argument("--ca-cert", default=None, help="Controller root CA certificate path. Defaults to the path from terraform.tfvars.")
    parser.add_argument("--ca-key", default=None, help="Controller root CA private key path. Defaults next to the CA certificate as root-ca.key.")
    parser.add_argument(
        "--smart-account-preconfigured",
        action="store_true",
        help="Advanced: skip the manual portal prompt and assume Cisco Services Registration is already completed on vManage.",
    )
    parser.add_argument("--poll-interval-seconds", type=int, default=10, help="Polling interval for asynchronous API waits.")
    parser.add_argument("--ready-timeout-seconds", type=int, default=2400, help="Timeout for registration, CSR, certificate install, and reachability waits.")
    args = parser.parse_args()

    module_dir = Path(args.module_dir).resolve()
    artifacts_dir = Path(args.artifacts_dir).resolve()
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    tfvars_path = module_dir / "terraform.tfvars"

    password = args.password or parse_tfvars_string(tfvars_path, "admin_password")
    organization_name = parse_tfvars_string(tfvars_path, "organization_name")
    vbond_hostname = parse_tfvars_string_or_default(tfvars_path, "vbond_hostname", "vbond.vbond") or "vbond.vbond"
    controller_certificate_method = (
        args.controller_certificate_method
        or parse_tfvars_string_or_default(
            tfvars_path,
            "controller_certificate_method",
            DEFAULT_CONTROLLER_CERTIFICATE_METHOD,
        )
        or DEFAULT_CONTROLLER_CERTIFICATE_METHOD
    ).strip().lower()
    if controller_certificate_method not in {"cisco_pki", "enterprise_local"}:
        raise RuntimeError(
            "controller_certificate_method must be one of: cisco_pki, enterprise_local"
        )

    ca_cert_path: Optional[Path] = None
    ca_key_path: Optional[Path] = None
    root_ca_text = ""
    if controller_certificate_method == "enterprise_local":
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

    log(
        f"Using primary vManage {config['primary_hostname']} at {config['primary_url']} "
        f"with controller_certificate_method={controller_certificate_method}"
    )
    confirm_certificate_enrollment(controller_certificate_method, args.yes)
    if controller_certificate_method == "cisco_pki":
        log_cisco_pki_prereqs(config, organization_name, vbond_hostname)
        run_cisco_pki_flow(
            session,
            config,
            None,
            None,
            organization_name,
            vbond_hostname,
            smart_account_preconfigured=args.smart_account_preconfigured,
        )
    else:
        if ca_cert_path is None or ca_key_path is None:
            raise RuntimeError("enterprise_local selected but controller root CA paths were not resolved")
        run_enterprise_local_flow(
            session,
            config,
            ca_cert_path,
            ca_key_path,
            root_ca_text,
            artifacts_dir,
        )

    log(f"Controller certificate API flow completed successfully via {controller_certificate_method}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

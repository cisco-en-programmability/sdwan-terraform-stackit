#!/usr/bin/env python3
"""Bootstrap or verify a 3-node vManage cluster.

This script is designed to run both:
- inside the primary vManage from cloud-init
- locally for post-deploy verification

The config file is a JSON document with at least:
{
  "username": "admin",
  "password": "...",
  "primary_url": "https://127.0.0.1",
  "poll_interval_seconds": 10,
  "server_ready_timeout_seconds": 1800,
  "cluster_ready_timeout_seconds": 2400,
  "services": {"sd-avc": {"server": false}},
  "state_file": "/var/lib/vmanage-cluster-bootstrap/configured",
  "nodes": [
    {
      "hostname": "stackittestuser-vmanage-01",
      "management_url": "https://10.0.0.11",
      "cluster_ip": "10.0.1.11",
      "system_ip": "10.255.0.11",
      "vmanage_id": "0",
      "persona": "COMPUTE_AND_DATA"
    }
  ]
}
"""

import argparse
import http.client
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

MODULE_DIR = Path(__file__).resolve().parents[1]


def log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


class VManageError(RuntimeError):
    pass


class VManageClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self._token = None  # type: Optional[str]
        self._logged_in = False
        self._cookie_jar = CookieJar()
        context = ssl._create_unverified_context()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context),
            urllib.request.HTTPCookieProcessor(self._cookie_jar),
        )

    def login(self) -> None:
        self._token = None
        self._logged_in = False
        login_payload = urllib.parse.urlencode(
            {
                "j_username": self.username,
                "j_password": self.password,
            }
        ).encode()
        request = urllib.request.Request(
            f"{self.base_url}/j_security_check",
            data=login_payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        body = self._read_response(request)
        if self._looks_like_login_page(body):
            raise VManageError(f"Login failed for {self.base_url}")
        if not any(cookie.name == "JSESSIONID" for cookie in self._cookie_jar):
            raise VManageError(f"Login did not return a JSESSIONID for {self.base_url}")

        token_request = urllib.request.Request(
            f"{self.base_url}/dataservice/client/token",
            headers={"Accept": "application/json"},
            method="GET",
        )
        token = self._read_response(token_request).strip().strip('"')
        if token:
            self._token = token
        self._logged_in = True

    def request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        for attempt in range(2):
            if not self._logged_in or attempt > 0:
                if attempt > 0:
                    log(f"Re-authenticating against {self.base_url}")
                self.login()

            try:
                return self._request_once(method, path, payload)
            except VManageError as exc:
                if "Authentication" not in str(exc) and "login page" not in str(exc):
                    raise
                self._logged_in = False
        raise VManageError(f"Authentication retries exhausted for {self.base_url}")

    def _request_once(self, method: str, path: str, payload: Optional[Dict[str, Any]]) -> Any:
        data = None if payload is None else json.dumps(payload).encode()
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if self._token:
            headers["X-XSRF-TOKEN"] = self._token

        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        body = self._read_response(request)
        if self._looks_like_login_page(body):
            raise VManageError(f"Authentication required for {self.base_url}{path}: received login page")
        if not body:
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise VManageError(f"Invalid JSON from {self.base_url}{path}: {body[:200]}") from exc

    def _read_response(self, request: urllib.request.Request) -> str:
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            if exc.code in (401, 403):
                raise VManageError(f"Authentication failed for {request.full_url}") from exc
            raise VManageError(f"HTTP {exc.code} for {request.full_url}: {body[:400]}") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, http.client.RemoteDisconnected) as exc:
            reason = getattr(exc, "reason", str(exc))
            raise VManageError(f"Request failed for {request.full_url}: {reason}") from exc

    @staticmethod
    def _looks_like_login_page(body: str) -> bool:
        lowered = body.lower()
        return "j_security_check" in lowered or "id=\"loginform\"" in lowered


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def parse_tfvars_string(tfvars_path: Path, key: str) -> str:
    pattern = re.compile(rf"^{re.escape(key)}\s*=\s*\"([^\"]*)\"", re.MULTILINE)
    match = pattern.search(tfvars_path.read_text(encoding="utf-8"))
    if not match:
        raise RuntimeError(f"Unable to find {key} in {tfvars_path}")
    return match.group(1)


def terraform_output(module_dir: Path, name: str) -> Any:
    result = subprocess.run(
        ["terraform", f"-chdir={module_dir}", "output", "-json", name],
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def choose_public_address(node: Dict[str, Any]) -> str:
    for key in ("management_public_ip", "transport_public_ip"):
        value = node.get(key)
        if isinstance(value, str) and value:
            return value
    raise RuntimeError(f"No public IP available for {node.get('hostname')}")


def build_config_from_terraform(
    module_dir: Path,
    username: str,
    password: str,
    poll_interval_seconds: int,
    server_ready_timeout_seconds: int,
    cluster_ready_timeout_seconds: int,
    state_file: Optional[str],
) -> Dict[str, Any]:
    inventory = terraform_output(module_dir, "controller_inventory")
    if not isinstance(inventory, dict):
        raise RuntimeError("controller_inventory output is not a map")

    vmanage_nodes = []
    for key, raw in sorted(inventory.items()):
        if not isinstance(raw, dict):
            continue
        if str(raw.get("role", "")) != "vmanage":
            continue
        cluster_ip = raw.get("cluster_ip")
        system_ip = raw.get("system_ip")
        hostname = raw.get("hostname", key)
        address = choose_public_address(raw)
        if not isinstance(cluster_ip, str) or not cluster_ip:
            raise RuntimeError(f"{key} is missing cluster_ip in controller_inventory")
        if not isinstance(system_ip, str) or not system_ip:
            raise RuntimeError(f"{key} is missing system_ip in controller_inventory")
        vmanage_nodes.append(
            {
                "key": key,
                "hostname": str(hostname),
                "management_url": f"https://{address}",
                "cluster_ip": cluster_ip,
                "system_ip": system_ip,
                "vmanage_id": "0" if key == "vmanage01" else "",
                "persona": "COMPUTE_AND_DATA",
            }
        )

    if len(vmanage_nodes) != 3:
        raise RuntimeError(
            f"Expected exactly 3 vManage nodes for single-tenant cluster formation, found {len(vmanage_nodes)}"
        )

    primary_url = vmanage_nodes[0]["management_url"]
    resolved_state_file = state_file or str(
        module_dir / "artifacts" / "vmanage-cluster-bootstrap" / "configured"
    )

    return {
        "username": username,
        "password": password,
        "primary_url": primary_url,
        "poll_interval_seconds": poll_interval_seconds,
        "server_ready_timeout_seconds": server_ready_timeout_seconds,
        "cluster_ready_timeout_seconds": cluster_ready_timeout_seconds,
        "services": {"sd-avc": {"server": False}},
        "state_file": resolved_state_file,
        "nodes": vmanage_nodes,
    }


def wait_for_https_listener(url: str, timeout: int, interval: int, label: str) -> None:
    deadline = time.monotonic() + timeout
    last_error = None  # type: Optional[str]
    request = urllib.request.Request(url, method="GET")
    context = ssl._create_unverified_context()

    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(request, context=context, timeout=10) as response:
                log(f"{label} HTTPS listener is reachable via {url} (HTTP {response.status})")
                return
        except urllib.error.HTTPError as exc:
            log(f"{label} HTTPS listener is reachable via {url} (HTTP {exc.code})")
            return
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        time.sleep(interval)

    raise TimeoutError(f"Timed out waiting for HTTPS listener on {label} via {url}: {last_error}")


def extract_cluster_entries(payload: Any) -> Dict[str, Dict[str, Any]]:
    data = payload.get("data", []) if isinstance(payload, dict) else []
    records = []  # type: List[Dict[str, Any]]
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict) and isinstance(first.get("data"), list):
            records = [item for item in first["data"] if isinstance(item, dict)]
        else:
            records = [item for item in data if isinstance(item, dict)]

    entries = {}  # type: Dict[str, Dict[str, Any]]
    for entry in records:
        config = entry.get("configJson", {})
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except json.JSONDecodeError:
                config = {}
        if not isinstance(config, dict):
            config = {}
        device_ip = config.get("deviceIP") or config.get("deviceIp")
        if not device_ip:
            continue
        entries[str(device_ip)] = {
            "id": str(entry.get("vmanageID", "")),
            "state": config.get("state"),
            "hostname": config.get("host-name") or config.get("hostname"),
            "config": config,
        }
    return entries


def extract_connected_device_ids(payload: Any) -> Set[str]:
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    ids = set()  # type: Set[str]
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            device_id = item.get("deviceId") or item.get("device_id")
            if device_id:
                ids.add(str(device_id))
    return ids


def extract_cluster_health_entries(payload: Any) -> Dict[str, Dict[str, bool]]:
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    records = data if isinstance(data, list) else []
    entries = {}  # type: Dict[str, Dict[str, bool]]
    for item in records:
        if not isinstance(item, dict):
            continue
        device_ip = item.get("deviceIP") or item.get("deviceIp")
        if not device_ip:
            continue
        services = {
            str(key): value
            for key, value in item.items()
            if key not in {"deviceIP", "deviceIp"} and isinstance(value, bool)
        }
        entries[str(device_ip)] = services
    return entries


def wait_for_server_ready(url: str, username: str, password: str, timeout: int, interval: int, label: str) -> None:
    deadline = time.monotonic() + timeout
    last_error = None  # type: Optional[str]
    while time.monotonic() < deadline:
        try:
            client = VManageClient(url, username, password)
            payload = client.request("GET", "/dataservice/client/server/ready")
            if isinstance(payload, dict) and payload.get("isServerReady"):
                log(f"{label} is ready via {url}")
                return
            last_error = f"unexpected readiness payload: {payload}"
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        time.sleep(interval)
    raise TimeoutError(f"Timed out waiting for {label} to become ready via {url}: {last_error}")


def wait_for_node_ready(node: Dict[str, Any], username: str, password: str, timeout: int, interval: int) -> None:
    management_url = str(node["management_url"])
    wait_for_https_listener(management_url, timeout, interval, node["hostname"])
    wait_for_server_ready(management_url, username, password, timeout, interval, node["hostname"])


def wait_for_all_nodes_ready(config: Dict[str, Any], interval: int) -> None:
    username = config["username"]
    password = config["password"]
    timeout = int(config["server_ready_timeout_seconds"])
    for node in config["nodes"]:
        wait_for_node_ready(node, username, password, timeout, interval)


def get_cluster_entries(primary_url: str, username: str, password: str) -> Dict[str, Dict[str, Any]]:
    client = VManageClient(primary_url, username, password)
    payload = client.request("GET", "/dataservice/clusterManagement/list")
    return extract_cluster_entries(payload)


def get_available_cluster_ips(url: str, username: str, password: str) -> Set[str]:
    client = VManageClient(url, username, password)
    payload = client.request("GET", "/dataservice/clusterManagement/iplist/0")
    if not isinstance(payload, list):
        return set()
    return {str(item) for item in payload if isinstance(item, str)}


def get_local_cluster_record(url: str, username: str, password: str) -> Dict[str, Any]:
    entries = get_cluster_entries(url, username, password)
    if len(entries) == 1:
        return next(iter(entries.values()))

    records = list(entries.values())
    for record in records:
        record_id = str(record.get("id") or "")
        if record_id in {"0", "1"}:
            return record

    if records:
        return records[0]

    raise RuntimeError(f"No clusterManagement/list record returned from {url}")


def wait_for_node_cluster_ip(
    node: Dict[str, Any],
    username: str,
    password: str,
    timeout: int,
    interval: int,
) -> None:
    deadline = time.monotonic() + timeout
    last_status = "cluster IP not checked yet"
    while time.monotonic() < deadline:
        try:
            wait_for_node_ready(node, username, password, interval * 3, interval)
            record = get_local_cluster_record(node["management_url"], username, password)
            current_ip = str(record.get("config", {}).get("deviceIP") or "")
            if current_ip == str(node["cluster_ip"]):
                log(f"{node['hostname']} now uses cluster IP {node['cluster_ip']}")
                return
            last_status = f"{node['hostname']} current cluster IP is {current_ip or 'unset'}"
        except Exception as exc:  # noqa: BLE001
            last_status = str(exc)
        time.sleep(interval)
    raise TimeoutError(f"Timed out waiting for {node['hostname']} cluster IP {node['cluster_ip']}: {last_status}")


def cluster_services_ready(
    primary_url: str,
    username: str,
    password: str,
    expected_nodes: List[Dict[str, Any]],
) -> Tuple[bool, str]:
    client = VManageClient(primary_url, username, password)
    entries = extract_cluster_health_entries(client.request("GET", "/dataservice/clusterManagement/health/status"))
    for node in expected_nodes:
        cluster_ip = node["cluster_ip"]
        service_status = entries.get(cluster_ip)
        if not service_status:
            return False, f"{cluster_ip} missing from cluster health status"
        failed = sorted(name for name, value in service_status.items() if value is False)
        if failed:
            return False, f"{cluster_ip} services not ready: {', '.join(failed)}"
    return True, "cluster services ready"


def cluster_ready(
    primary_url: str,
    username: str,
    password: str,
    expected_nodes: List[Dict[str, Any]],
) -> Tuple[bool, str]:
    client = VManageClient(primary_url, username, password)
    entries = extract_cluster_entries(client.request("GET", "/dataservice/clusterManagement/list"))
    for node in expected_nodes:
        cluster_ip = node["cluster_ip"]
        system_ip = node["system_ip"]
        entry = entries.get(cluster_ip)
        if not entry:
            return False, f"{cluster_ip} missing from cluster list"
        state = str(entry.get("state") or "").lower()
        if state and state != "ready":
            return False, f"{cluster_ip} is present but state={entry.get('state')}"
        try:
            connected = extract_connected_device_ids(
                client.request("GET", f"/dataservice/clusterManagement/connectedDevices/{cluster_ip}")
            )
            if connected and system_ip not in connected:
                return False, f"{cluster_ip} is present but system-ip {system_ip} is not connected yet"
        except Exception as exc:  # noqa: BLE001
            log(f"Connected-device check for {cluster_ip} was inconclusive: {exc}")
    try:
        services_ready, service_status = cluster_services_ready(primary_url, username, password, expected_nodes)
        if not services_ready:
            return False, service_status
    except Exception as exc:  # noqa: BLE001
        return False, f"cluster health status check failed: {exc}"
    return True, "cluster ready"


def wait_for_cluster_ready(
    primary_url: str,
    username: str,
    password: str,
    expected_nodes: List[Dict[str, Any]],
    timeout: int,
    interval: int,
) -> None:
    deadline = time.monotonic() + timeout
    last_status = "cluster not checked yet"
    while time.monotonic() < deadline:
        try:
            wait_for_https_listener(primary_url, interval * 3, interval, "primary vManage")
            wait_for_server_ready(primary_url, username, password, interval * 3, interval, "primary vManage")
            ready, status = cluster_ready(primary_url, username, password, expected_nodes)
            last_status = status
            if ready:
                log(f"Cluster is ready for {len(expected_nodes)} vManage node(s)")
                return
        except Exception as exc:  # noqa: BLE001
            last_status = str(exc)
        time.sleep(interval)
    raise TimeoutError(f"Timed out waiting for cluster readiness: {last_status}")


def ensure_node_cluster_ip(node: Dict[str, Any], username: str, password: str, timeout: int, interval: int, services: Dict[str, Any]) -> None:
    wait_for_node_ready(node, username, password, timeout, interval)

    available_ips = get_available_cluster_ips(node["management_url"], username, password)
    if str(node["cluster_ip"]) not in available_ips:
        raise RuntimeError(
            f"{node['hostname']} cluster IP {node['cluster_ip']} not present in clusterManagement/iplist/0: "
            f"{sorted(available_ips)}"
        )

    record = get_local_cluster_record(node["management_url"], username, password)
    current_ip = str(record.get("config", {}).get("deviceIP") or "")
    if current_ip == str(node["cluster_ip"]):
        log(f"{node['hostname']} already uses cluster IP {node['cluster_ip']}")
        return

    client = VManageClient(node["management_url"], username, password)
    payload = {
        "vmanageID": str(record.get("id") or node.get("vmanage_id") or "0"),
        "deviceIP": node["cluster_ip"],
        "username": username,
        "password": password,
        "persona": node.get("persona", "COMPUTE_AND_DATA"),
        "services": services,
    }
    log(f"Editing {node['hostname']} to cluster IP {node['cluster_ip']}")
    try:
        client.request("PUT", "/dataservice/clusterManagement/setup", payload)
    except Exception as exc:  # noqa: BLE001
        log(
            f"{node['hostname']} cluster-IP change request did not return a clean response; "
            f"continuing with readiness polling because application-server may be restarting: {exc}"
        )
    wait_for_node_cluster_ip(node, username, password, timeout, interval)


def prepare_primary_cluster_ip(config: Dict[str, Any], interval: int) -> None:
    username = config["username"]
    password = config["password"]
    timeout = int(config["server_ready_timeout_seconds"])
    services = config.get("services", {"sd-avc": {"server": False}})
    primary = config["nodes"][0]
    ensure_node_cluster_ip(primary, username, password, timeout, interval, services)


def ensure_additional_members(config: Dict[str, Any], interval: int) -> None:
    primary = config["nodes"][0]
    username = config["username"]
    password = config["password"]
    primary_url = config["primary_url"]
    services = config.get("services", {"sd-avc": {"server": False}})

    existing_entries = get_cluster_entries(primary_url, username, password)
    current_member_ips = {primary["cluster_ip"]}
    current_members = [primary]
    for node in config["nodes"][1:]:
        if node["cluster_ip"] in existing_entries:
            current_member_ips.add(node["cluster_ip"])
            current_members.append(node)

    for node in config["nodes"][1:]:
        wait_for_node_ready(node, username, password, int(config["server_ready_timeout_seconds"]), interval)

        entries = get_cluster_entries(primary_url, username, password)
        existing = entries.get(node["cluster_ip"])
        if existing:
            log(f"{node['hostname']} already present in cluster as {node['cluster_ip']}")
            if node["cluster_ip"] not in current_member_ips:
                current_member_ips.add(node["cluster_ip"])
                current_members.append(node)
            wait_for_cluster_ready(
                primary_url,
                username,
                password,
                current_members,
                config["cluster_ready_timeout_seconds"],
                interval,
            )
            continue

        client = VManageClient(primary_url, username, password)
        payload = {
            "deviceIP": node["cluster_ip"],
            "username": username,
            "password": password,
            "genCSR": False,
            "persona": node.get("persona", "COMPUTE_AND_DATA"),
            "services": services,
        }
        log(f"Adding {node['hostname']} to cluster using cluster IP {node['cluster_ip']}")
        try:
            client.request("POST", "/dataservice/clusterManagement/setup", payload)
        except Exception as exc:  # noqa: BLE001
            log(
                f"Add-node request for {node['hostname']} did not return a clean response; "
                f"continuing with readiness polling because application-server may be restarting: {exc}"
            )
        if node["cluster_ip"] not in current_member_ips:
            current_member_ips.add(node["cluster_ip"])
            current_members.append(node)
        wait_for_cluster_ready(
            primary_url,
            username,
            password,
            current_members,
            config["cluster_ready_timeout_seconds"],
            interval,
        )


def write_success_marker(config: Dict[str, Any]) -> None:
    state_file = config.get("state_file")
    if not state_file:
        return
    path = Path(state_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(time.strftime("%Y-%m-%d %H:%M:%S") + "\n", encoding="utf-8")


def confirm_cluster_formation(config: Dict[str, Any], auto_approve: bool) -> None:
    if auto_approve:
        return

    primary = config["nodes"][0]
    print("Planned vManage cluster formation:", flush=True)
    print(
        f"  primary: {primary['hostname']} via {primary['management_url']} with cluster IP {primary['cluster_ip']}",
        flush=True,
    )
    for node in config["nodes"][1:]:
        print(
            f"  add: {node['hostname']} via {node['management_url']} with cluster IP {node['cluster_ip']}",
            flush=True,
        )
    print(
        "This operation may restart application-server and may reboot cluster members while services resync.",
        flush=True,
    )
    response = input("Type 'yes' to continue with 3-node cluster formation: ").strip()
    if response != "yes":
        raise RuntimeError("Cluster formation cancelled by operator")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to the cluster bootstrap JSON config file")
    parser.add_argument(
        "--module-dir",
        default=str(MODULE_DIR),
        help="Terraform module directory used to derive the 3-node vManage config when --config is not provided.",
    )
    parser.add_argument("--username", default="admin", help="vManage username when deriving config from Terraform.")
    parser.add_argument("--password", default="", help="vManage password. Defaults to admin_password in terraform.tfvars.")
    parser.add_argument("--poll-interval-seconds", type=int, default=30, help="Polling interval for readiness checks.")
    parser.add_argument("--server-ready-timeout-seconds", type=int, default=7200, help="Timeout for HTTPS and /server/ready checks.")
    parser.add_argument("--cluster-ready-timeout-seconds", type=int, default=10800, help="Timeout for cluster convergence after each mutation.")
    parser.add_argument("--state-file", default="", help="Optional local state-file path used to skip repeat cluster formation.")
    parser.add_argument("--yes", action="store_true", help="Skip the operator confirmation prompt before cluster mutation.")
    parser.add_argument("--verify-only", action="store_true", help="Only verify cluster readiness")
    args = parser.parse_args()

    if args.config:
        config = load_config(args.config)
    else:
        module_dir = Path(args.module_dir).resolve()
        tfvars_path = module_dir / "terraform.tfvars"
        password = args.password or parse_tfvars_string(tfvars_path, "admin_password")
        config = build_config_from_terraform(
            module_dir=module_dir,
            username=args.username,
            password=password,
            poll_interval_seconds=args.poll_interval_seconds,
            server_ready_timeout_seconds=args.server_ready_timeout_seconds,
            cluster_ready_timeout_seconds=args.cluster_ready_timeout_seconds,
            state_file=args.state_file or None,
        )

    interval = int(config.get("poll_interval_seconds", args.poll_interval_seconds))
    username = config["username"]
    password = config["password"]
    primary_url = config["primary_url"]

    try:
        wait_for_all_nodes_ready(config, interval)

        if args.verify_only:
            wait_for_cluster_ready(
                primary_url,
                username,
                password,
                config["nodes"],
                int(config["cluster_ready_timeout_seconds"]),
                interval,
            )
            return 0

        try:
            wait_for_cluster_ready(
                primary_url,
                username,
                password,
                config["nodes"],
                interval * 2,
                interval,
            )
            log("vManage cluster is already ready; no cluster mutation is required")
            write_success_marker(config)
            return 0
        except Exception:
            pass

        state_file = config.get("state_file")
        if state_file and os.path.exists(state_file):
            log(f"Cluster bootstrap marker already exists at {state_file}; verifying current state")
            wait_for_cluster_ready(
                primary_url,
                username,
                password,
                config["nodes"],
                int(config["cluster_ready_timeout_seconds"]),
                interval,
            )
            return 0

        confirm_cluster_formation(config, args.yes)
        prepare_primary_cluster_ip(config, interval)
        ensure_additional_members(config, interval)
        wait_for_cluster_ready(
            primary_url,
            username,
            password,
            config["nodes"],
            int(config["cluster_ready_timeout_seconds"]),
            interval,
        )
        write_success_marker(config)
        log("vManage cluster bootstrap completed successfully")
        return 0
    except Exception as exc:  # noqa: BLE001
        log(f"vManage cluster bootstrap failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

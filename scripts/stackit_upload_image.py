#!/usr/bin/env python3
"""Upload Cisco controller images to STACKIT and print Terraform-ready IDs.

This helper wraps `stackit image create` for the controller image types used by
this repository:
- `vmanage`
- `vsmart`
- `vbond`

The script uploads any subset of those images, then prints an `image_ids` block
you can paste into `terraform.tfvars`.

Notes:
- It relies on an already-authenticated local `stackit` CLI.
- It is intentionally a CLI wrapper, not a separate STACKIT API client.
- The uploaded image names should be unique enough that an exact-name lookup
  resolves to a single image after upload.
- The qcow2 inputs are expected to come from software.cisco.com under:
  SDWAN > vManage Software / vSmart Software / vEdge Cloud > vBond Software.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


ROLE_ORDER = ("vmanage", "vsmart", "vbond")
DEFAULT_LABELS = {
    "image_origin": "vendor",
    "product": "cisco-sdwan",
}


def log(message: str) -> None:
    print(message, flush=True)


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, text=True, capture_output=True, check=True)
    except subprocess.CalledProcessError as exc:
        stdout = exc.stdout.strip()
        stderr = exc.stderr.strip()
        details = [f"Command failed with exit code {exc.returncode}: {' '.join(exc.cmd)}"]
        if stdout:
            details.append(f"stdout:\n{stdout}")
        if stderr:
            details.append(f"stderr:\n{stderr}")
        raise RuntimeError("\n".join(details)) from exc


def parse_json_output(stdout: str) -> Any:
    data = stdout.strip()
    if not data:
        return {}
    try:
        return json.loads(data)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Expected JSON output from stackit CLI, got:\n{data[:800]}") from exc


def extract_image_id(payload: Any) -> Optional[str]:
    if isinstance(payload, dict):
        for key in ("id", "imageId", "imageID", "image_id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        for value in payload.values():
            found = extract_image_id(value)
            if found:
                return found
    if isinstance(payload, list):
        for item in payload:
            found = extract_image_id(item)
            if found:
                return found
    return None


def extract_rows(payload: Any) -> list[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        data = payload.get("items", payload.get("data", payload.get("images")))
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    return []


def image_name_matches(row: Dict[str, Any], expected_name: str) -> bool:
    for key in ("name", "displayName", "imageName"):
        value = row.get(key)
        if isinstance(value, str) and value == expected_name:
            return True
    return False


def format_labels(labels: Dict[str, str]) -> str:
    return ",".join(f"{key}={value}" for key, value in sorted(labels.items()))


def stackit_global_flags(project_id: Optional[str], region: Optional[str]) -> list[str]:
    flags: list[str] = []
    if project_id:
        flags.extend(["--project-id", project_id])
    if region:
        flags.extend(["--region", region])
    return flags


def resolve_image_id_by_name(name: str, project_id: Optional[str], region: Optional[str]) -> str:
    cmd = ["stackit", "image", "list", "-o", "json", *stackit_global_flags(project_id, region)]
    payload = parse_json_output(run(cmd).stdout)
    matches = [row for row in extract_rows(payload) if image_name_matches(row, name)]
    if not matches:
        raise RuntimeError(f"Image upload completed but no image named {name!r} was found in `stackit image list`.")
    if len(matches) > 1:
        raise RuntimeError(
            f"More than one image named {name!r} was found. Use a unique image name and rerun the upload."
        )
    image_id = extract_image_id(matches[0])
    if not image_id:
        raise RuntimeError(f"Unable to extract an image ID for {name!r} from stackit image list output.")
    return image_id


def upload_image(
    *,
    role: str,
    path: Path,
    name: str,
    disk_format: str,
    project_id: Optional[str],
    region: Optional[str],
    architecture: Optional[str],
    os_name: Optional[str],
    os_distro: Optional[str],
    os_version: Optional[str],
    min_disk_size: Optional[int],
    min_ram: Optional[int],
    uefi: Optional[bool],
    virtio_scsi: bool,
    protected: bool,
    no_progress: bool,
) -> str:
    if not path.exists():
        raise RuntimeError(f"{role} image path does not exist: {path}")
    if not path.is_file():
        raise RuntimeError(f"{role} image path is not a file: {path}")

    labels = dict(DEFAULT_LABELS)
    labels.update({"role": role})

    cmd = [
        "stackit",
        "image",
        "create",
        "--name",
        name,
        "--disk-format",
        disk_format,
        "--local-file-path",
        str(path),
        "--labels",
        format_labels(labels),
        "-o",
        "json",
        "-y",
        *stackit_global_flags(project_id, region),
    ]
    if architecture:
        cmd.extend(["--architecture", architecture])
    if os_name:
        cmd.extend(["--os", os_name])
    if os_distro:
        cmd.extend(["--os-distro", os_distro])
    if os_version:
        cmd.extend(["--os-version", os_version])
    if min_disk_size is not None:
        cmd.extend(["--min-disk-size", str(min_disk_size)])
    if min_ram is not None:
        cmd.extend(["--min-ram", str(min_ram)])
    if uefi is not None:
        cmd.append(f"--uefi={'true' if uefi else 'false'}")
    if virtio_scsi:
        cmd.append("--virtio-scsi")
    if protected:
        cmd.append("--protected")
    if no_progress:
        cmd.append("--no-progress")

    log(f"Uploading {role} image {path} as STACKIT image {name!r}")
    payload = parse_json_output(run(cmd).stdout)
    image_id = extract_image_id(payload)
    if image_id:
        return image_id
    return resolve_image_id_by_name(name, project_id, region)


def append_role(
    uploads: Dict[str, Dict[str, str]],
    role: str,
    raw_path: Optional[str],
    raw_name: Optional[str],
) -> None:
    if not raw_path:
        return
    path = Path(raw_path).expanduser().resolve()
    name = raw_name or path.stem
    uploads[role] = {"path": str(path), "name": name}


def emit_tfvars_block(results: Dict[str, str]) -> None:
    log("")
    log("Terraform image_ids block:")
    log("image_ids = {")
    for role in ROLE_ORDER:
        if role in results:
            log(f'  {role:<8} = "{results[role]}"')
    log("}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Upload vManage, vSmart, and vBond images to STACKIT and print Terraform-ready image_ids."
    )
    parser.add_argument("--project-id", default=None, help="Optional STACKIT project ID. Defaults to the active CLI/project context.")
    parser.add_argument("--region", default=None, help="Optional STACKIT region. Defaults to the active CLI/project context.")
    parser.add_argument("--disk-format", default="qcow2", help="Image disk format passed to stackit image create. Defaults to qcow2.")
    parser.add_argument("--architecture", default=None, help="Optional CPU architecture override for stackit image create.")
    parser.add_argument("--os", dest="os_name", default=None, help="Optional OS value passed to stackit image create.")
    parser.add_argument("--os-distro", default=None, help="Optional OS distro value passed to stackit image create.")
    parser.add_argument("--os-version", default=None, help="Optional OS version value passed to stackit image create.")
    parser.add_argument("--min-disk-size", type=int, default=None, help="Optional minimum disk size in GB.")
    parser.add_argument("--min-ram", type=int, default=None, help="Optional minimum RAM in MB.")
    parser.add_argument("--uefi", dest="uefi", action="store_true", help="Force UEFI on for uploaded images.")
    parser.add_argument("--no-uefi", dest="uefi", action="store_false", help="Force UEFI off for uploaded images.")
    parser.add_argument("--virtio-scsi", action="store_true", help="Enable VirtIO SCSI during image upload.")
    parser.add_argument("--protected", action="store_true", help="Mark uploaded images as protected.")
    parser.add_argument("--no-progress", action="store_true", help="Disable the STACKIT CLI upload progress bar.")
    parser.add_argument("--vmanage-path", default=None, help="Local vManage image path.")
    parser.add_argument("--vmanage-name", default=None, help="STACKIT image name for vManage. Defaults to the file stem.")
    parser.add_argument("--vsmart-path", default=None, help="Local vSmart image path.")
    parser.add_argument("--vsmart-name", default=None, help="STACKIT image name for vSmart. Defaults to the file stem.")
    parser.add_argument("--vbond-path", default=None, help="Local vBond image path.")
    parser.add_argument("--vbond-name", default=None, help="STACKIT image name for vBond. Defaults to the file stem.")
    parser.set_defaults(uefi=None)
    args = parser.parse_args()

    uploads: Dict[str, Dict[str, str]] = {}
    append_role(uploads, "vmanage", args.vmanage_path, args.vmanage_name)
    append_role(uploads, "vsmart", args.vsmart_path, args.vsmart_name)
    append_role(uploads, "vbond", args.vbond_path, args.vbond_name)
    if not uploads:
        parser.error("At least one of --vmanage-path, --vsmart-path, or --vbond-path is required.")

    results: Dict[str, str] = {}
    for role in ROLE_ORDER:
        current = uploads.get(role)
        if not current:
            continue
        image_id = upload_image(
            role=role,
            path=Path(current["path"]),
            name=current["name"],
            disk_format=args.disk_format,
            project_id=args.project_id,
            region=args.region,
            architecture=args.architecture,
            os_name=args.os_name,
            os_distro=args.os_distro,
            os_version=args.os_version,
            min_disk_size=args.min_disk_size,
            min_ram=args.min_ram,
            uefi=args.uefi,
            virtio_scsi=args.virtio_scsi,
            protected=args.protected,
            no_progress=args.no_progress,
        )
        results[role] = image_id
        log(f"{role} uploaded successfully with image ID {image_id}")

    emit_tfvars_block(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

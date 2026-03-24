# Repo Guidance

This repository provisions a fixed Cisco SD-WAN controller lab on STACKIT:

- 3 `vManage`
- 2 `vBond`
- 2 `vSmart`

## Core Files

- `main.tf`, `locals.tf`, `variables.tf`, `outputs.tf`: Terraform topology, defaults, and outputs.
- `cloud-init/vmanage-rootca.yaml.tftpl`, `cloud-init/vbond-rootca.yaml.tftpl`, `cloud-init/vsmart-rootca.yaml.tftpl`: active wrapper templates used by the current Terraform flow.
- `cloud-init/vmanage.xml.tftpl`, `cloud-init/vbond.xml.tftpl`, `cloud-init/vsmart.xml.tftpl`: active working day-0 XML payloads used by the current Terraform flow.
- `scripts/stackit_disk_format.py`: published `/dev/vdb` first-boot handling with strict `/opt/data` validation.
- `scripts/stackit_cluster_certificate.py`: published post-deploy wrapper for 3-node vManage cluster formation followed by controller certificate enrollment.
- `scripts/bootstrap_vmanage_cluster.py`, `scripts/cert_api_script.py`: lower-level implementation scripts used by the wrapper and still useful for debugging.
- `scripts/teardown_stackit_lab.sh`: preferred destroy helper when plain `terraform destroy` gets stuck.

## Operator Expectations

- `organization_name` must match the value used on `software.cisco.com`.
- `vbond_hostname` must be a DNS-resolvable vBond FQDN. It must match the value configured in `software.cisco.com > Network Plug and Play > Controller Profiles`.
- `admin_access_cidrs` should contain only external operator/admin source ranges. Controller-to-controller reachability is added automatically by Terraform.
- `run_vmanage_firstboot_init` should remain `false` by default so the user can verify the Terraform deployment first and run the first-boot helper independently.

## Script and Output Model

- The Python scripts read Terraform outputs, especially `controller_inventory`, from the module directory.
- By default they assume the repo root is the module directory.
- If the repo is copied elsewhere, use `--module-dir /absolute/path/to/repo` for the Python scripts that support it.
- Shell helpers derive paths from the repo they live in, so run them from the checkout you want to operate on.
- The Python helpers are expected to run from a local virtual environment in the repo, using `python3 -m venv .venv` and `python3 -m pip install -r requirements.txt`.
- `requirements.txt` currently contains the pip-managed runtime dependency set for the repo scripts. Non-Python tools such as Terraform, OpenSSL, SSH, and shell utilities are separate host prerequisites.

## Troubleshooting Patterns Seen So Far

- STACKIT API resets can interrupt `terraform apply`; rerunning `terraform apply` is the normal recovery path.
- `vManage` can look reachable before `/dev/vdb` formatting is actually complete; trust `/opt/data` validation, not early HTTPS.
- Cisco Services Registration may show correctly in the portal even when older Smart Account APIs return empty objects; the current cert workflow treats the `ciscoServices` Plug-and-Play row as authoritative on this build.
- `vSmart` and `vBond` onboarding on this build works through management public IPs.
- Secondary `vManage` CSR generation needs the node’s own API endpoint rather than only the primary cluster API.
- Raw destroy can still get stuck on `vManage` data-volume detach; prefer `scripts/teardown_stackit_lab.sh`.

## Safety

- Do not commit local secrets, generated certificates, `terraform.tfvars`, Terraform state, or `artifacts/`.
- Do not destroy or replace a live lab without explicit confirmation from the user.

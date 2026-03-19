# Repo Guidance

This repository provisions a fixed Cisco SD-WAN lab on STACKIT:

- 3 `vManage`
- 2 `vBond`
- 2 `vSmart`

## Important Files

- `main.tf`, `locals.tf`, `variables.tf`, `outputs.tf`: Terraform topology and outputs.
- `cloud-init/`: role-specific day-0 templates.
- `scripts/format_vmanage_data_disks.py`: parallel `/dev/vdb` first-boot handling for all vManage nodes.
- `scripts/bootstrap_vmanage_cluster.py`: 3-node vManage cluster formation.
- `scripts/cert_api_script.py`: add `vSmart`/`vBond`, generate controller CSRs through vManage APIs, sign locally, install signed certs through vManage APIs, and wait for controller reachability.
- `scripts/legacy/post_deploy_controllers.py`: legacy direct-device controller cert flow kept as a fallback.
- `scripts/legacy/add_controllers_to_vmanage.py`: legacy controller registration helper kept as a fallback.
- `scripts/teardown_stackit_lab.sh`: preferred teardown path when plain `terraform destroy` gets stuck on vManage data-volume detach.

## Local-Only Inputs

These files are intentionally not committed and must stay local:

- `terraform.tfvars`
- `certs/`
- `.terraform/`
- `terraform.tfstate*`
- `artifacts/`

Use `terraform.tfvars.example` as the shareable baseline.

## Expected Bring-Up Flow

Run the lab in this order:

1. `terraform init`
2. `terraform plan`
3. `terraform apply`
4. `python3 ./scripts/format_vmanage_data_disks.py`
5. `python3 ./scripts/bootstrap_vmanage_cluster.py`
6. `python3 ./scripts/cert_api_script.py`

`terraform apply` should not automatically run the interactive vManage first-boot helper. Keep:

- `run_vmanage_firstboot_init = false`

If `/dev/vdb` still needs manual handling later, use `scripts/init_vmanage_firstboot.sh` explicitly.

## SD-WAN-Specific Expectations

- vManage uses three NICs: management, transport, cluster/OOB.
- vManage cluster membership must use the private cluster/OOB IPs, not public IPs.
- `vManage` and `vSmart` should use:
  - `vbond vbond.vbond`
  - `host vbond.vbond ip <vbond01-transport-ip> <vbond02-transport-ip>`
- Controller cloud-init is root-CA-only; controller identity certificates are generated and installed later through vManage APIs.
- Controller site IDs are per-node, not shared.

## StackIT Provider Quirks

- The STACKIT provider may emit the false-positive warning:
  - `No network interfaces configured`
  This warning has been observed during valid plans/applies.
- vManage extra data-volume detach is the most common destroy failure.
  Prefer `scripts/teardown_stackit_lab.sh` over raw `terraform destroy` when tearing down a live lab.

## Safety

- Do not destroy or replace the running lab without explicit user confirmation.
- Do not commit local secrets, generated certs, `terraform.tfvars`, or Terraform state.

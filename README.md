# STACKIT SD-WAN Topology

This repository provisions a fixed Cisco SD-WAN controller lab on STACKIT:

- 3 `vManage`
- 2 `vBond`
- 2 `vSmart`

The current published flow covers Terraform deployment, vManage `/dev/vdb` first-boot formatting, 3-node vManage cluster formation, and controller certificate enrollment through vManage APIs.

## What Terraform Deploys

- management, transport, and vManage cluster networks
- separate management, transport, and cluster security groups
- controller public-IP peer allowlisting on management and transport
- explicit transport ingress for SD-WAN control port `12346`
- fixed private NIC IPs on STACKIT while keeping controller interfaces on DHCP in the guest
- public IPs on controller management and transport NICs by default
- day-0 cloud-init for `vManage`, `vBond`, and `vSmart`
- one extra data disk on each `vManage`

By default, controller certificates use `cisco_pki`. That means the built-in Cisco trust bundle in the image is left untouched during Terraform deployment, and the certificate workflow is completed later through `./scripts/stackit_cluster_certificate.py`.

## Before You Start

- Upload the controller images to STACKIT first and capture the image IDs.
- Download the controller qcow2 images first from [software.cisco.com](https://software.cisco.com/download/home/) under `SDWAN`:
  - `vManage Software`
  - `vSmart Software`
  - `vEdge Cloud`, then use the `vBond Software` image from that section
- Make sure the Cisco Smart Account organization and Plug and Play controller profile already exist on [software.cisco.com](https://software.cisco.com/).
- Set `organization_name` to the exact organization name used on the Cisco portal.
- Set `vbond_hostname` to the vBond FQDN used in the Cisco controller profile. The default is `vbond.vbond`.

If you want a repo helper for the image-upload stage, use:

```sh
python3 ./scripts/stackit_upload_image.py \
  --vmanage-path /absolute/path/to/vmanage.qcow2 \
  --vsmart-path /absolute/path/to/vsmart.qcow2 \
  --vbond-path /absolute/path/to/vbond.qcow2
```

The helper wraps `stackit image create` and prints the resulting `image_ids = { ... }` block in the format expected by `terraform.tfvars`.
Use the qcow2 files downloaded from `software.cisco.com > SDWAN > vManage Software / vSmart Software / vEdge Cloud > vBond Software`.

`vbond_hostname` is not just a label. It must:

- be a real vBond FQDN
- be DNS resolvable from the controller VMs
- match what you configure in `software.cisco.com > Network Plug and Play > Controller Profiles`
- match the vBond FQDN you expect the controllers to use at runtime

## Authentication

Use the official STACKIT Terraform provider authentication flow. A local service account key file is the simplest setup:

```sh
export STACKIT_SERVICE_ACCOUNT_KEY_PATH=/absolute/path/to/service-account-key.json
```

If your service account was created with your own RSA key pair, also export:

```sh
export STACKIT_PRIVATE_KEY_PATH=/absolute/path/to/private-key.pem
```

## Python Environment

The Python helpers in `scripts/` are intended to run from a local virtual environment.

Use Python `3.11` or newer, then create and activate a virtual environment:

```sh
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

The checked-in `requirements.txt` currently installs the Python package used by the script set:

- `requests`

That virtual environment covers Python packages only. Install these tools separately on the host:

- `terraform`
- `openssl`
- `bash`
- `ssh`
- `curl`
- `nc`

If you keep the repo in a different folder, activate the virtual environment from that copied checkout before running the Python scripts there.

## Inputs

1. Copy the example file:

```sh
cp terraform.tfvars.example terraform.tfvars
```

2. Fill in at least:

- `project_id`
- `organization_name`
- `vbond_hostname`
- `image_ids`
- `machine_types`
- `admin_password`
- `admin_password_hash`
- `admin_access_cidrs`

Important input notes:

- `organization_name` must match the organization name used on `software.cisco.com`.
- `vbond_hostname` must be the DNS-resolvable vBond FQDN and must match the value configured in the Cisco Plug and Play controller profile.
- `admin_access_cidrs` should contain the external operator/admin source ranges that need access to the controller public IPs.
- Access between the controller instances themselves is added automatically by Terraform. Do not put internal controller IPs into `admin_access_cidrs`.
- `run_vmanage_firstboot_init` defaults to `false` so you can first confirm `terraform apply` completed successfully and then run the disk-formatting helper independently.

Appliance VM labels include the mandatory SentinelOne exemption block on every `stackit_server` resource:

- `image_origin = "vendor"`
- `product = "cisco-sdwan"`
- `s1risk = "RK0027865"`

## Certificate Methods

`controller_certificate_method` defaults to `cisco_pki`.

Use `cisco_pki` when:

- you want Cisco PKI to sign controller certificates
- your Smart Account and controller profile are already prepared on `software.cisco.com`
- `organization_name` and `vbond_hostname` match the Cisco portal values
- you want Terraform/cloud-init to leave the default Cisco trust bundle in the image untouched

Use `enterprise_local` only if you explicitly want to keep a local shared controller CA and sign controller certificates yourself.

## Active Templates and Variations

The current Terraform flow uses these active templates:

- `cloud-init/vmanage-rootca.yaml.tftpl`
- `cloud-init/vbond-rootca.yaml.tftpl`
- `cloud-init/vsmart-rootca.yaml.tftpl`
- `cloud-init/vmanage.xml.tftpl`
- `cloud-init/vbond.xml.tftpl`
- `cloud-init/vsmart.xml.tftpl`

Those are the current working templates. The other files under `cloud-init/` are retained as named variations from earlier experiments, compatibility tests, or legacy flows.

## How The Scripts Use Terraform Outputs

The Python scripts derive controller addresses and metadata from Terraform outputs, especially:

- `controller_inventory`
- `vmanage_urls`
- `primary_vbond_transport_ip`

By default, the Python scripts assume the Terraform module directory is the repository root. These scripts support `--module-dir` so you can point them at a different checkout or copied folder:

- `scripts/stackit_disk_format.py`
- `scripts/stackit_cluster_certificate.py`
- `scripts/stackit_upload_image.py`
- `scripts/add_controllers_to_vmanage.py`
- `scripts/post_deploy_controllers.py`

Examples:

```sh
python3 ./scripts/stackit_disk_format.py --module-dir /absolute/path/to/sdwan-terraform-stackit
python3 ./scripts/stackit_cluster_certificate.py --module-dir /absolute/path/to/sdwan-terraform-stackit
```

The shell helpers use paths relative to the repo they are run from, so if you copied the repo elsewhere, run those helpers from that copied checkout.

If you keep multiple checkouts, make sure the active virtual environment and the `--module-dir` value refer to the same repo copy. The scripts read Terraform outputs from `--module-dir`, not from the shell's current working directory alone.

## Apply And Bring-Up

Run the Terraform stage first:

```sh
terraform init
terraform plan
terraform apply
```

Keep this setting disabled for the standard manual workflow:

```hcl
run_vmanage_firstboot_init = false
```

That is the intended default. It lets you verify the infrastructure deployment before triggering the interactive first-boot disk handling.

### 1. Format the vManage data disks

```sh
python3 ./scripts/stackit_disk_format.py
```

This script:

- reads the `vManage` nodes from Terraform `controller_inventory`
- runs the `/dev/vdb` first-boot flow in parallel
- waits for each node to confirm `/opt/data` is mounted as a separate filesystem

### 2. Form the 3-node vManage cluster and enroll controller certificates

```sh
python3 ./scripts/stackit_cluster_certificate.py
```

This wrapper:

- runs 3-node vManage cluster formation first
- then runs controller certificate enrollment
- is safe to rerun because the underlying cluster and certificate stages are rerunnable

If `controller_certificate_method = "cisco_pki"`, the certificate stage:

- pauses for manual Cisco Services Registration when needed
- expects the Smart Account settings on vManage to match `organization_name`
- expects the vBond FQDN on the Cisco controller profile to match `vbond_hostname`
- adds `vSmart` and `vBond` through vManage APIs
- generates controller CSRs through vManage APIs
- waits for Cisco PKI to install the certificates
- syncs vSmart certs to vBond and verifies `vSmart`/`vBond` reachability

If `controller_certificate_method = "enterprise_local"`, the same wrapper falls back to the enterprise-local certificate stage, which:

  - uploads the local controller root CA to vManage
  - reads CSRs from vManage APIs
  - signs them locally
  - installs the signed certificates back through vManage APIs

### 4. Legacy Fallback

Legacy flows are kept under `scripts/legacy/` as a safety net:

- `scripts/legacy/post_deploy_controllers.py`
- `scripts/legacy/add_controllers_to_vmanage.py`

## Teardown

Prefer the teardown helper over a raw `terraform destroy`:

```sh
bash ./scripts/teardown_stackit_lab.sh
```

It retries the normal destroy flow and, if needed, stops `vManage` nodes and detaches their data volumes before retrying.

## Troubleshooting

Common issues seen so far:

- STACKIT API resets during `terraform apply`
  - Symptom: server create or poll fails late with transport reset or transient API errors.
  - Action: rerun `terraform apply`. The current graph is safe to continue from partial state.

- `vManage` HTTPS is up but `/dev/vdb` formatting is not actually complete
  - Symptom: login still shows storage formatting prompts on one or more managers.
  - Action: rerun `python3 ./scripts/stackit_disk_format.py`. The current script validates `/opt/data` instead of trusting early HTTPS.

- Cisco Services Registration looks complete in the portal but some older APIs return empty objects
  - Symptom: `smartaccountcredentials` or `pnpConnectSync` returns `{}` while the portal clearly shows Plug-and-Play registered.
  - Action: rely on the current certificate stage inside `stackit_cluster_certificate.py`, which now treats the `ciscoServices` Plug-and-Play registration row as the authoritative signal on this build.

- `vSmart` or `vBond` add fails through vManage
  - Symptom: `Unable to connect to admin@...:830`.
  - Action: on this build, controller onboarding works through the management public IP path. The current script already prefers that path.

- Secondary `vManage` CSR generation behaves differently than the primary
  - Symptom: CSR generation from the primary API says it cannot find the device.
  - Action: the current script uses each secondary `vManage` node’s own API endpoint for CSR generation.

- Teardown gets stuck on `vManage` volume detach
  - Symptom: raw `terraform destroy` hangs or fails repeatedly near the data disks.
  - Action: use `bash ./scripts/teardown_stackit_lab.sh`.

- STACKIT provider warning about `No network interfaces configured`
  - Symptom: `terraform validate` or other provider operations emit that warning for `stackit_server.controller`.
  - Action: this warning is incorrect for this repo. Terraform still creates and attaches the controller management, transport, and cluster network interfaces as defined in the plan. Treat it as a known STACKIT provider false-positive and validate the actual plan or apply outcome instead of treating the warning alone as fatal.

## Notes

- `vManage` and `vSmart` use `vbond.vbond` by default and inject both vBond transport IPs into `vpn 0 host`.
- Access between controller instances is created automatically during Terraform deployment on both public and private paths needed by this lab.
- `network_ipv4_nameservers` defaults to `["1.1.1.1", "8.8.8.8"]`; set it to `null` if you want the STACKIT network defaults instead.
- Controller site IDs are configured per node, not as one shared value.
- The checked-in `terraform.tfvars.example` is only a template. Keep your local `terraform.tfvars`, `certs/`, `.terraform/`, Terraform state, and `artifacts/` out of version control.
- If you want a single stable front door for the 3-node vManage cluster, you can also place a load balancer in front of the managers. That is optional for this repo flow, but it can be useful for operator access and external integrations.

See [CONTRIBUTIONS.md](/Users/ssalemar/sdwan-terraform-stackit/CONTRIBUTIONS.md) for contribution contact details.

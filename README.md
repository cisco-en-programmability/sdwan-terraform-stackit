# STACKIT SD-WAN Topology

This module provisions a fixed Cisco SD-WAN lab on STACKIT:

- 3 `vManage`
- 2 `vSmart`
- 2 `vBond`
- 2 `c8000v`

It assumes you have already uploaded the four custom images with `stackit image create` and have the resulting image IDs.

## What It Does

- Creates four networks:
  - management
  - transport
  - vManage cluster
  - service
- Creates separate management and transport security groups
- Provisions fixed private IPs on the STACKIT NICs while leaving the controller management/transport interfaces on DHCP inside the guest
- Allocates public IPs on every management and transport NIC by default
- Bootstraps controllers with STACKIT `user_data` cloud-init
- Bootstraps the c8000v nodes with day-0 cloud-init
- Creates and attaches an extra block volume to each vManage node
- Stages locally generated or user-provided cert artifacts into the controller and edge guests when configured
- Keeps the interactive vManage first-boot helper out of normal `terraform apply` unless you explicitly enable it

Interface layout:

- `vManage`: management (`eth0`, DHCP), transport (`eth1`, DHCP), cluster (`eth2`, static, private only)
- `vSmart` / `vBond`: management (`eth0`, DHCP), transport (`eth1`, DHCP)
- `c8000v`: management (`GigabitEthernet1`, DHCP), transport (`GigabitEthernet2`, DHCP), service (`GigabitEthernet3`, static)

All instance names are prefixed with `shivram` by default.

## Important Boundary

This scaffold gets the infrastructure and day-0 bootstrap in place. Terraform by itself does **not** fully automate the 20.18 controller onboarding steps that are still needed to get to a production-like SD-WAN overlay:

- vManage cluster formation
- adding the vBond/vSmart controllers into vManage
- c8000v onboarding/device authorization in vManage
- device template attachment and policy push

That boundary is intentional. Terraform can reliably provision the topology, but it should not invent the cluster-registration workflow without your specific org, CA, Smart Account, serial/UUID, and vManage preferences.

## Authentication

Use the official STACKIT Terraform provider authentication flow. The simplest local setup is a service account key file:

```sh
export STACKIT_SERVICE_ACCOUNT_KEY_PATH=/absolute/path/to/service-account-key.json
```

If your service account key was created with your own RSA key pair, also provide:

```sh
export STACKIT_PRIVATE_KEY_PATH=/absolute/path/to/private-key.pem
```

## Inputs

1. Copy the example file:

```sh
cp terraform.tfvars.example terraform.tfvars
```

2. Fill in:

- `project_id`
- `organization_name`
- the four uploaded `image_ids`
- the three `machine_types`
- `admin_password`
- `admin_password_hash`
- `admin_access_cidrs`

Controller site IDs are configured per node, not as one shared value:

- `vmanage_site_ids = [110, 111, 112]`
- `vbond_site_ids = [120, 121]`
- `vsmart_site_ids = [130, 131]`

The c8000v nodes continue to use `edge_site_ids`.

Certificate files for the current vManage cloud-init install workflow now live under:

- `certs/vmanage/root-ca.crt`
- `certs/vmanage/server.crt`
- `certs/vmanage/server.key`
- `certs/vmanage/server.csr`
- `certs/vmanage/symantec-root-ca.crt`

The example and active `terraform.tfvars` both point to those local module paths now.

Generate the controller hash with:

```sh
openssl passwd -6 'your-password'
```

## vManage Certificate Sources

The module supports three vManage cert modes via `vmanage_cert_mode`:

- `generated`: create a local example root CA and vManage server cert bundle with `openssl`, then install those files through cloud-init
- `provided`: install your own cert files from the paths in `vmanage_*_cert_path`
- `disabled`: do not inject cert artifacts through cloud-init

### Generated Example Certs

When `vmanage_cert_mode = "generated"`, Terraform runs:

- [generate_vmanage_example_certs.sh](/Users/ssalemar/terraform-sdwan-modules/stackit/scripts/generate_vmanage_example_certs.sh)

That script writes an example bundle under `vmanage_generated_cert_dir`, which defaults to:

- `certs/vmanage/generated/root-ca.key`
- `certs/vmanage/generated/root-ca.crt`
- `certs/vmanage/generated/server.key`
- `certs/vmanage/generated/server.csr`
- `certs/vmanage/generated/server.crt`

The generated files are intended for lab bring-up and testing. They are not a production certificate workflow.
For a real 3-node vManage cluster, prefer `vmanage_cert_mode = "provided"` with per-node signed certs that match your controller identity workflow.

### Bring Your Own Certs

To use your own files instead:

1. Set `vmanage_cert_mode = "provided"`.
2. Point these variables at your files:
   - `vmanage_root_ca_cert_path`
   - `vmanage_server_cert_path`
   - `vmanage_server_key_path`
   - `vmanage_server_csr_path`
3. Apply Terraform again.

The vManage cloud-init template writes the selected files into `/usr/share/viptela/` on the guest and runs the certificate install commands during first boot.

## vBond, vSmart, and c8000v Cert Installation

The module can also install role-specific cert bundles for:

- `vBond`
- `vSmart`
- `c8000v`

Those roles use path-based inputs:

- `vbond_root_ca_cert_path`
- `vbond_server_cert_path`
- `vbond_server_key_path`
- `vbond_server_csr_path`
- `vsmart_root_ca_cert_path`
- `vsmart_server_cert_path`
- `vsmart_server_key_path`
- `vsmart_server_csr_path`
- `c8000v_root_ca_cert_path`
- `c8000v_server_cert_path`
- `c8000v_server_key_path`
- `c8000v_server_csr_path`

The active example config points those variables at generated local bundles:

- `certs/vbond/generated/*`
- `certs/vsmart/generated/*`
- `certs/c8000v/generated/*`

Regenerate those example files with [generate_vmanage_example_certs.sh](/Users/ssalemar/terraform-sdwan-modules/stackit/scripts/generate_vmanage_example_certs.sh), or replace the paths with your own cert material. The active templates now write controller certs to `/usr/share/viptela/` and c8000v certs to `bootflash:`, then run the corresponding install commands from cloud-init.

The active `terraform.tfvars` now targets the full overlay:

- 3 `vManage`
- 2 `vBond`
- 2 `vSmart`
- 2 `c8000v`

and uses generated local example cert bundles for all roles by default.

## Apply

```sh
terraform init
terraform plan
terraform apply
```

For a normal manual bring-up flow, keep this disabled:

```hcl
run_vmanage_firstboot_init = false
```

That is the current default in both [variables.tf](/Users/ssalemar/terraform-sdwan-modules/stackit/variables.tf) and [terraform.tfvars](/Users/ssalemar/terraform-sdwan-modules/stackit/terraform.tfvars).

If you later want to run the helper manually for a specific vManage node, use:

```sh
bash /Users/ssalemar/terraform-sdwan-modules/stackit/scripts/init_vmanage_firstboot.sh <public-ip> '<admin-password>'
```

To complete the controller certificate flow after `terraform apply`, run:

```sh
python3 /Users/ssalemar/terraform-sdwan-modules/stackit/scripts/post_deploy_controllers.py
```

That script:

- runs the vManage `/dev/vdb` first-boot helper in parallel across all three vManage nodes
- detects when the storage prompt is already complete and proceeds without waiting on the full vManage GUI stack
- enforces `vbond ${vbond_hostname}` plus `vpn 0 host ${vbond_hostname} ip <vbond01> <vbond02>` on `vManage` and `vSmart`
- installs the shared root CA on all `vManage`, `vBond`, and `vSmart` controllers
- generates CSRs on each controller, signs them locally with unique certificate serial numbers, installs the signed certs, and verifies `certificate-status Installed`
- is safe to rerun if a previous attempt partially completed

To form the 3-node single-tenant vManage cluster after the controller cert flow is complete, run:

```sh
python3 /Users/ssalemar/terraform-sdwan-modules/stackit/scripts/bootstrap_vmanage_cluster.py
```

That script:

- derives the three vManage nodes directly from Terraform `controller_inventory`
- waits for HTTPS reachability and `/dataservice/client/server/ready` on all three vManage nodes before any cluster mutation
- prompts you to type `yes` before changing the primary cluster IP or adding the other two vManage nodes
- follows the `adab`/`sdwan_rest` cluster flow for 20.18:
  - `PUT /dataservice/clusterManagement/setup` for the primary cluster IP
  - `POST /dataservice/clusterManagement/setup` for each additional vManage
  - patient waits for application-server restarts, cluster sync, and cluster health readiness after each step
- is safe to rerun; if the cluster is already ready it exits without making changes

To add the `vSmart` and `vBond` controllers into vManage after the cluster is ready, run:

```sh
python3 /Users/ssalemar/terraform-sdwan-modules/stackit/scripts/add_controllers_to_vmanage.py
```

That script:

- derives the target `vSmart` and `vBond` nodes directly from Terraform `controller_inventory`
- follows the `adab` controller bring-up order:
  - add `vSmart` first with `POST /dataservice/system/device`
  - add `vBond` next with `POST /dataservice/system/device`
  - force `generateCSR = false` because the controller certs are already installed on the nodes
  - trigger `POST /dataservice/certificate/vsmart/list` to sync certificates to vBond
- waits until vManage reports the selected controllers as reachable with expected control connections up through `/dataservice/device/reachable?personality=...`
- is safe to rerun; already-registered controllers are skipped and the script can be used in `--verify-only` mode

## Outputs

Useful outputs after apply:

- `vmanage_urls`
- `controller_inventory`
- `edge_inventory`
- `primary_vbond_transport_ip`

## Post-Provision Checks

After the first apply, verify:

- the three vManage nodes have `/opt/data` mounted
- both management and transport public IPs exist if you enabled them
- `https://<vmanage management public ip>` is reachable

## Next Steps After Terraform

Use the output inventory to complete the SD-WAN onboarding flow:

1. Run `python3 scripts/post_deploy_controllers.py`.
2. Run `python3 scripts/bootstrap_vmanage_cluster.py`.
3. Run `python3 scripts/add_controllers_to_vmanage.py`.
4. Authorize/onboard the two c8000v nodes.
5. Attach templates and push policy so the edges join the overlay.

## Notes

- `vManage` and `vSmart` bootstrap now point at `vbond.vbond`, and day-0 injects `vpn 0 host vbond.vbond ip <vbond01 transport ip> <vbond02 transport ip>` so both vBond transport IPs are available from first boot.
- The management and transport networks keep DHCP enabled. Because each STACKIT NIC has a fixed private IP, the guests receive deterministic DHCP leases plus a gateway and nameservers from the network.
- The default `terraform.tfvars` pins `network_ipv4_nameservers` to `["1.1.1.1", "8.8.8.8"]` so the DHCP clients on management and transport always receive resolvers. If you prefer the STACKIT network-area defaults instead, set the variable back to `null`.
- When you provide an explicit network CIDR, the module pins the gateway to the first usable IPv4 address in that subnet. If you let STACKIT allocate a free subnet, STACKIT still creates the gateway and the module exposes it through `network_inventory`.
- The repo now keeps two vManage cloud-init variants:
  - `cloud-init/vmanage-basic-working.yaml.tftpl`: the last known-good minimal template without cert injection
  - `cloud-init/vmanage-basic-certs.yaml.tftpl`: the current active template that writes the generated/provided cert bundle into the runtime cert paths and installs it from cloud-init
- The repo also keeps backup and active cert templates for the other roles:
  - `cloud-init/vbond-working.yaml.tftpl` and `cloud-init/vbond-certs.yaml.tftpl`
  - `cloud-init/vsmart-working.yaml.tftpl` and `cloud-init/vsmart-certs.yaml.tftpl`
  - `cloud-init/c8000v-working.yaml.tftpl` and `cloud-init/c8000v-certs.yaml.tftpl`
- The active cert templates no longer use a staging directory. They write to the live cert locations and run the install commands from cloud-init itself.
- The generated example cert flow uses `openssl` locally through `scripts/generate_vmanage_example_certs.sh`, then injects the resulting files with the same cloud-init path as provided certs.
- The checked-in `terraform.tfvars` is currently set for the full 3 vManage / 2 vBond / 2 vSmart / 2 c8000v overlay. To scope a one-off test deployment, set `enabled_controller_keys` and/or `enabled_edge_keys` to explicit lists such as `["vmanage01"]`.
- The generated local cert flow is role-scoped today: one generated bundle for `vmanage`, one for `vbond`, one for `vsmart`, and one for `c8000v`. That is sufficient for staging and first-boot validation, but for a real controller cluster use per-node signed certs via the `provided` paths.
- The large `symantec-root-ca.crt` is intentionally not embedded in `user_data`, because it exceeds STACKIT's user-data size limit when combined with the day-0 config and other staged files.
- The local `certs/` directory is git-ignored so the private key and other local cert material stay out of version control.
- The vManage data disk is still attached before the first real boot by creating the server `inactive`, attaching the extra volume, then starting the node once. That is the closest Terraform/provider-safe equivalent to inline extra-volume server creation on STACKIT.
- The repository still includes `scripts/bootstrap_vmanage_cluster.py`, but Terraform does not auto-run it. Bring up and validate the standalone vManage nodes first, then run cluster formation explicitly when you are ready.
- `scripts/bootstrap_vmanage_cluster.py` now derives the 3-node vManage plan from Terraform output by default, so you do not need to hand-author a JSON config for the common single-tenant lab case.
- `scripts/bootstrap_vmanage_cluster.py` now follows the `adab` cluster workflow more closely by first preparing each node's OOB/cluster IP on that node itself, then adding the secondary nodes to the primary.
- The controller cert flow is now handled by `scripts/post_deploy_controllers.py`, not by waiting for the vManage GUI to become healthy first.
- If you want a stricter or more internet-exposed underlay policy, adjust the management and transport security group rules in `main.tf`.

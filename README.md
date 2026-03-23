# STACKIT SD-WAN Topology

This module provisions a fixed Cisco SD-WAN lab on STACKIT:

- 3 `vManage`
- 2 `vSmart`
- 2 `vBond`

It assumes you have already uploaded the three custom controller images with `stackit image create` and have the resulting image IDs.

## What It Does

- Creates three networks:
  - management
  - transport
  - vManage cluster
- Creates separate management and transport security groups
- Allows every controller public IP to reach every other controller public IP on management and transport from the initial Terraform deployment
- Opens SD-WAN control port `12346` explicitly on the transport security group
- Provisions fixed private IPs on the STACKIT NICs while leaving the controller management/transport interfaces on DHCP inside the guest
- Allocates public IPs on every management and transport NIC by default
- Bootstraps controllers with STACKIT `user_data` cloud-init
- Creates and attaches an extra block volume to each vManage node
- Defaults controller certificates to Cisco PKI, leaving the built-in Cisco trust bundle in the image untouched
- Keeps an `enterprise_local` fallback that can inject a shared controller root CA through cloud-init
- Keeps the interactive vManage first-boot helper out of normal `terraform apply` unless you explicitly enable it

Interface layout:

- `vManage`: management (`eth0`, DHCP), transport (`eth1`, DHCP), cluster (`eth2`, static, private only)
- `vSmart` / `vBond`: management (`eth0`, DHCP), transport (`eth1`, DHCP)

All instance names are prefixed with `stackittestuser` by default.

## Important Boundary

This scaffold now automates the controller-side lifecycle through a small set of manual post-deploy scripts, but it still stops short of a full end-to-end production overlay:

- device template attachment and policy push
- WAN-edge onboarding and policy configuration above the base controller fabric

That boundary is intentional. Terraform can reliably provision the topology, and the bundled scripts can reliably finish the controller fabric, but the repo should not invent the full edge onboarding and policy workflow without your specific Smart Account, serial/UUID inventory, and vManage preferences.

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
- the three uploaded `image_ids`
- the two `machine_types`
- `admin_password`
- `admin_password_hash`
- `admin_access_cidrs`

Appliance VM labels now include a mandatory SentinelOne exemption block on every `stackit_server` resource:

- `image_origin = "vendor"`
- `product = "cisco-sdwan"`
- `s1risk = "RK0027865"`

If you want to add extra labels on the appliance VM resources, use `custom_labels`. The older `labels` input still applies to the other supported STACKIT resources in the module.

Controller site IDs are configured per node, not as one shared value:

- `vmanage_site_ids = [110, 111, 112]`
- `vbond_site_ids = [120, 121]`
- `vsmart_site_ids = [130, 131]`

The default controller certificate flow is `cisco_pki`:

- no root CA is injected through cloud-init
- `./scripts/cert_api_script.py` prompts for Cisco Smart Account credentials at runtime
- the Smart Account organization must match `organization_name`

The fallback `enterprise_local` controller certificate flow uses a shared root CA across all controllers:

- `certs/controllers/root-ca.crt`
- `certs/controllers/root-ca.key`

Generate the controller hash with:

```sh
openssl passwd -6 'your-password'
```

## Controller Certificate Methods

`controller_certificate_method` defaults to `cisco_pki`.

Use the default when:

- you want vManage to use Cisco PKI
- you have a Cisco Smart Account whose organization matches `organization_name`
- you do not want to inject a local controller root CA during cloud-init

If you switch to `enterprise_local`, the controller root CA must exist locally before the certificate flow runs.

Generate it with:

```sh
bash ./scripts/generate_controller_root_ca.sh \
  --output-dir ./certs/controllers \
  --org STACKITTESTUSER_SDWAN \
  --root-cn 'STACKITTESTUSER_SDWAN Controller Root CA' \
  --valid-days 3650
```

The controller cloud-init templates are:

- `cloud-init/vmanage-rootca.yaml.tftpl`
- `cloud-init/vbond-rootca.yaml.tftpl`
- `cloud-init/vsmart-rootca.yaml.tftpl`

They always provide day-0 config. They only inject the root CA when `controller_certificate_method = "enterprise_local"`.

That keeps the boot workflow simple:

- Terraform/cloud-init provides day-0 config.
- `/dev/vdb` formatting is handled next.
- vManage cluster formation happens after the data disks are ready.
- Controller identity certificates are then handled through `./scripts/cert_api_script.py` using either Cisco PKI or the enterprise-local fallback.

The older direct-device certificate flow is kept under `scripts/legacy/` as a fallback while the API flow is being validated.

## Apply and Bring-Up

```sh
terraform init
terraform plan
terraform apply
```

For a normal manual bring-up flow, keep this disabled:

```hcl
run_vmanage_firstboot_init = false
```

That is the current default in `variables.tf` and `terraform.tfvars`.

If you later want to run the helper manually for a specific vManage node, use:

```sh
bash ./scripts/init_vmanage_firstboot.sh <public-ip> '<admin-password>'
```

To complete the active bring-up flow after `terraform apply`, run the steps in this order.

### 1. Format the vManage data disks

```sh
python3 ./scripts/format_vmanage_data_disks.py
```

That script:

- runs the vManage `/dev/vdb` first-boot helper in parallel across all three vManage nodes
- does not treat early HTTPS as proof that first-boot storage is complete
- only returns success after each node confirms `/opt/data` is mounted as a separate filesystem

### 2. Form the 3-node vManage cluster

```sh
python3 ./scripts/bootstrap_vmanage_cluster.py
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

### 3. Add `vSmart` and `vBond`, generate CSRs through vManage, sign locally, and install the signed certs

```sh
python3 ./scripts/cert_api_script.py
```

If `controller_certificate_method = "cisco_pki"` which is the default, that script:

- prompts for Cisco Smart Account username and password without storing the password on disk
- validates the Smart Account configuration on vManage
- adds `vSmart` first and `vBond` second with `POST /dataservice/system/device`
- generates controller CSRs through vManage APIs in this order:
  - `vmanage01`
  - `vmanage02`
  - `vmanage03`
  - `vbond01`
  - `vbond02`
  - `vsmart01`
  - `vsmart02`
- waits for Cisco PKI to install the controller certificates
- triggers `POST /dataservice/certificate/vsmart/list` after the installs so vBond receives the updated vSmart certificate information
- waits until vManage reports the targeted `vSmart` and `vBond` nodes as reachable and UP

If `controller_certificate_method = "enterprise_local"`, the same script falls back to the previous flow:

- uploads the shared enterprise controller root CA into vManage settings
- pulls the CSR PEMs back from vManage certificate inventory APIs
- signs them locally with the shared controller root CA using unique serial numbers
- installs the signed controller certificates back through `/dataservice/certificate/install/signedCert`

### Legacy Fallback

The older direct-device controller cert flow is preserved under `scripts/legacy/`:

- `scripts/legacy/post_deploy_controllers.py`
- `scripts/legacy/add_controllers_to_vmanage.py`

## Teardown

Prefer the helper script over raw `terraform destroy`:

```sh
bash ./scripts/teardown_stackit_lab.sh
```

That helper retries the usual destroy flow and, if needed, stops the vManage nodes and detaches their extra data volumes with the `stackit` CLI before retrying.

## Outputs

Useful outputs after apply:

- `vmanage_urls`
- `controller_inventory`
- `primary_vbond_transport_ip`

## Post-Provision Checks

After the first apply, verify:

- the three vManage nodes have `/opt/data` mounted
- both management and transport public IPs exist if you enabled them
- `https://<vmanage management public ip>` is reachable

## Next Steps After Terraform

Use the output inventory to complete the SD-WAN onboarding flow:

1. Run `python3 scripts/format_vmanage_data_disks.py`.
2. Run `python3 scripts/bootstrap_vmanage_cluster.py`.
3. Run `python3 scripts/cert_api_script.py`.
4. Authorize/onboard WAN edges and attach templates when you are ready.

## Notes

- `vManage` and `vSmart` bootstrap now point at `vbond.vbond`, and day-0 injects `vpn 0 host vbond.vbond ip <vbond01 transport ip> <vbond02 transport ip>` so both vBond transport IPs are available from first boot.
- The management and transport networks keep DHCP enabled. Because each STACKIT NIC has a fixed private IP, the guests receive deterministic DHCP leases plus a gateway and nameservers from the network.
- The default `terraform.tfvars` pins `network_ipv4_nameservers` to `["1.1.1.1", "8.8.8.8"]` so the DHCP clients on management and transport always receive resolvers. If you prefer the STACKIT network-area defaults instead, set the variable back to `null`.
- When you provide an explicit network CIDR, the module pins the gateway to the first usable IPv4 address in that subnet. If you let STACKIT allocate a free subnet, STACKIT still creates the gateway and the module exposes it through `network_inventory`.
- The active controller cloud-init templates are day-0-only by default and only inject a root CA when `controller_certificate_method = "enterprise_local"`:
  - `cloud-init/vmanage-rootca.yaml.tftpl`
  - `cloud-init/vbond-rootca.yaml.tftpl`
  - `cloud-init/vsmart-rootca.yaml.tftpl`
- The older direct-device controller cert flow is preserved under `scripts/legacy/` while the new API-driven flow is being validated.
- The checked-in `terraform.tfvars` is currently set for the full 3 vManage / 2 vBond / 2 vSmart controller overlay. To scope a one-off test deployment, set `enabled_controller_keys` to an explicit list such as `["vmanage01"]`.
- When `controller_certificate_method = "enterprise_local"`, the shared controller root CA path in the current local config is `certs/controllers/root-ca.crt`. The matching private key stays local at `certs/controllers/root-ca.key` and is used later by `scripts/cert_api_script.py`.
- The large `symantec-root-ca.crt` is intentionally not embedded in controller `user_data`, because it exceeds STACKIT's user-data size limit when combined with the day-0 config.
- The local `certs/` directory is git-ignored so the private key and other local cert material stay out of version control.
- The vManage data disk is still attached before the first real boot by creating the server `inactive`, attaching the extra volume, then starting the node once. That is the closest Terraform/provider-safe equivalent to inline extra-volume server creation on STACKIT.
- The repository still includes `scripts/bootstrap_vmanage_cluster.py`, but Terraform does not auto-run it. Bring up and validate the standalone vManage nodes first, then run cluster formation explicitly when you are ready.
- `scripts/bootstrap_vmanage_cluster.py` now derives the 3-node vManage plan from Terraform output by default, so you do not need to hand-author a JSON config for the common single-tenant lab case.
- `scripts/bootstrap_vmanage_cluster.py` uses the cluster/OOB IPs from Terraform output for membership, reaches each node through its management URL, and now prefers the safer flow of preparing the primary first and then adding only missing secondary nodes from the primary.
- The active controller cert flow is now handled by `scripts/cert_api_script.py`, which adds `vSmart`/`vBond`, generates CSRs through vManage APIs, signs them locally, and installs the signed certificates back through vManage APIs.
- The default underlay policy now includes controller-public-IP peer allowlisting on both management and transport, plus explicit `12346/TCP` and `12346/UDP` ingress on transport for SD-WAN control-plane bring-up.
- If you want a stricter or more internet-exposed underlay policy, adjust the management and transport security group rules in `main.tf`.

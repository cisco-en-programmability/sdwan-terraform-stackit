variable "project_id" {
  description = "STACKIT project ID."
  type        = string
}

variable "region" {
  description = "STACKIT region."
  type        = string
  default     = "eu01"
}

variable "availability_zone" {
  description = "STACKIT availability zone used for all servers and volumes."
  type        = string
  default     = "eu01-1"
}

variable "prefix" {
  description = "Resource name prefix."
  type        = string
  default     = "stackittestuser"
}

variable "organization_name" {
  description = "Cisco SD-WAN organization name."
  type        = string
}

variable "domain_id" {
  description = "Cisco SD-WAN domain ID used by all nodes."
  type        = number
  default     = 1
}

variable "vbond_port" {
  description = "vBond control port."
  type        = number
  default     = 12346
}

variable "vbond_hostname" {
  description = "Hostname used by controllers to resolve the vBond transport endpoints."
  type        = string
  default     = "vbond.vbond"
}

variable "image_ids" {
  description = "Image IDs created ahead of time with stackit image create."
  type = object({
    vmanage = string
    vsmart  = string
    vbond   = string
  })
}

variable "machine_types" {
  description = "STACKIT machine types per device class."
  type = object({
    vmanage    = string
    controller = string
  })
}

variable "boot_volume_sizes" {
  description = "Boot volume sizes in GB."
  type = object({
    vmanage    = number
    controller = number
  })
  default = {
    vmanage    = 30
    controller = 12
  }
}

variable "vmanage_data_disk_size" {
  description = "Additional data disk size for each vManage node in GB."
  type        = number
  default     = 100
}

variable "vmanage_data_volume_performance_class" {
  description = "Optional STACKIT block storage performance class for the vManage data disks."
  type        = string
  default     = null
  nullable    = true
}

variable "vmanage_prestart_settle_seconds" {
  description = "Seconds to wait after the vManage data disk attach before the first server start."
  type        = number
  default     = 45
}

variable "admin_password" {
  description = "Plain-text admin password used for SD-WAN bootstrap scripts and API workflows."
  type        = string
  sensitive   = true
}

variable "admin_password_hash" {
  description = "SHA-512 password hash used for controller bootstrap. Example: openssl passwd -6 '<password>'."
  type        = string
  sensitive   = true
}

variable "ssh_public_key" {
  description = "Optional SSH public key added to controller cloud-init."
  type        = string
  default     = ""
}

variable "controller_certificate_method" {
  description = "Controller certificate automation method: cisco_pki or enterprise_local. cisco_pki is the default and uses the Cisco PKI flow after deploy."
  type        = string
  default     = "cisco_pki"

  validation {
    condition     = contains(["cisco_pki", "enterprise_local"], var.controller_certificate_method)
    error_message = "controller_certificate_method must be one of: cisco_pki, enterprise_local."
  }
}

variable "vmanage_cert_mode" {
  description = "How vManage certificate artifacts are sourced: generated, provided, or disabled."
  type        = string
  default     = "disabled"

  validation {
    condition     = contains(["generated", "provided", "disabled"], var.vmanage_cert_mode)
    error_message = "vmanage_cert_mode must be one of: generated, provided, disabled."
  }
}

variable "vmanage_generated_cert_dir" {
  description = "Directory where example vManage certificate artifacts are generated locally when vmanage_cert_mode is generated."
  type        = string
  default     = "certs/vmanage/generated"
}

variable "vmanage_generated_root_common_name" {
  description = "Root CA common name for generated example vManage certificates."
  type        = string
  default     = ""
}

variable "vmanage_generated_server_common_name" {
  description = "Server certificate common name for generated example vManage certificates."
  type        = string
  default     = ""
}

variable "vmanage_generated_cert_validity_days" {
  description = "Validity period in days for generated example vManage certificates."
  type        = number
  default     = 3650
}

variable "vmanage_symantec_root_ca_cert_path" {
  description = "Optional local path to a Symantec root CA certificate written to /usr/share/viptela/symantec-root-ca.crt on vManage nodes."
  type        = string
  default     = ""
}

variable "vmanage_root_ca_cert_path" {
  description = "Optional local path to the vManage root CA certificate written to /usr/share/viptela/root-ca.crt."
  type        = string
  default     = ""
}

variable "vmanage_server_cert_path" {
  description = "Optional local path to the vManage server certificate written to /usr/share/viptela/server.crt."
  type        = string
  default     = ""
}

variable "vmanage_server_key_path" {
  description = "Optional local path to the vManage server private key written to /usr/share/viptela/server.key."
  type        = string
  default     = ""
}

variable "vmanage_server_csr_path" {
  description = "Optional local path to the vManage server CSR written to /usr/share/viptela/server.csr."
  type        = string
  default     = ""
}

variable "vbond_root_ca_cert_path" {
  description = "Optional local path to the vBond root CA certificate written to /usr/share/viptela/root-ca.crt."
  type        = string
  default     = ""
}

variable "vbond_server_cert_path" {
  description = "Optional local path to the vBond server certificate written to /usr/share/viptela/server.crt."
  type        = string
  default     = ""
}

variable "vbond_server_key_path" {
  description = "Optional local path to the vBond server private key written to /usr/share/viptela/server.key."
  type        = string
  default     = ""
}

variable "vbond_server_csr_path" {
  description = "Optional local path to the vBond server CSR written to /usr/share/viptela/server.csr."
  type        = string
  default     = ""
}

variable "vsmart_root_ca_cert_path" {
  description = "Optional local path to the vSmart root CA certificate written to /usr/share/viptela/root-ca.crt."
  type        = string
  default     = ""
}

variable "vsmart_server_cert_path" {
  description = "Optional local path to the vSmart server certificate written to /usr/share/viptela/server.crt."
  type        = string
  default     = ""
}

variable "vsmart_server_key_path" {
  description = "Optional local path to the vSmart server private key written to /usr/share/viptela/server.key."
  type        = string
  default     = ""
}

variable "vsmart_server_csr_path" {
  description = "Optional local path to the vSmart server CSR written to /usr/share/viptela/server.csr."
  type        = string
  default     = ""
}

variable "admin_access_cidrs" {
  description = "CIDRs allowed to reach the management and transport public IPs for SSH and HTTPS."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for cidr in var.admin_access_cidrs : can(cidrnetmask(cidr))])
    error_message = "Each admin_access_cidrs entry must be a valid CIDR, for example 203.0.113.10/32."
  }
}

variable "ssh_access_cidrs" {
  description = "CIDRs allowed to reach the management and transport public IPs for SSH. If empty, admin_access_cidrs are used."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for cidr in var.ssh_access_cidrs : can(cidrnetmask(cidr))])
    error_message = "Each ssh_access_cidrs entry must be a valid CIDR, for example 0.0.0.0/0."
  }
}

variable "management_public_ips_enabled" {
  description = "Whether to allocate public IPs on every management NIC."
  type        = bool
  default     = true
}

variable "transport_public_ips_enabled" {
  description = "Whether to allocate public IPs on every transport NIC."
  type        = bool
  default     = true
}

variable "run_vmanage_firstboot_init" {
  description = "Whether Terraform should run scripts/init_vmanage_firstboot.sh against vManage after the VM starts. Leave false for manual bring-up."
  type        = bool
  default     = false
}

variable "management_network_cidr" {
  description = "Management network CIDR."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.management_network_cidr == null ? true : can(cidrhost(var.management_network_cidr, 1))
    error_message = "management_network_cidr must be null or a valid CIDR."
  }
}

variable "transport_network_cidr" {
  description = "Transport network CIDR."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.transport_network_cidr == null ? true : can(cidrhost(var.transport_network_cidr, 1))
    error_message = "transport_network_cidr must be null or a valid CIDR."
  }
}

variable "cluster_network_cidr" {
  description = "Private vManage cluster network CIDR."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.cluster_network_cidr == null ? true : can(cidrhost(var.cluster_network_cidr, 1))
    error_message = "cluster_network_cidr must be null or a valid CIDR."
  }
}

variable "system_ip_cidr" {
  description = "CIDR used only to allocate SD-WAN system IPs."
  type        = string
  default     = "10.255.0.0/24"

  validation {
    condition     = can(cidrhost(var.system_ip_cidr, 1))
    error_message = "system_ip_cidr must be a valid CIDR."
  }
}

variable "management_network_prefix_length" {
  description = "Management network prefix length used when management_network_cidr is null."
  type        = number
  default     = 25
}

variable "transport_network_prefix_length" {
  description = "Transport network prefix length used when transport_network_cidr is null."
  type        = number
  default     = 25
}

variable "cluster_network_prefix_length" {
  description = "vManage cluster network prefix length used when cluster_network_cidr is null."
  type        = number
  default     = 25
}

variable "network_ipv4_nameservers" {
  description = "Optional IPv4 nameservers for the STACKIT networks. Set to null to use the network area's default nameservers."
  type        = list(string)
  default     = null
  nullable    = true

  validation {
    condition     = var.network_ipv4_nameservers == null ? true : alltrue([for ip in var.network_ipv4_nameservers : can(cidrhost("${ip}/32", 0))])
    error_message = "Each network_ipv4_nameservers entry must be a valid IPv4 address."
  }
}

variable "vmanage_site_ids" {
  description = "Site IDs for the three vManage nodes."
  type        = list(number)
  default     = [110, 111, 112]

  validation {
    condition     = length(var.vmanage_site_ids) == 3
    error_message = "vmanage_site_ids must contain exactly three values."
  }
}

variable "vbond_site_ids" {
  description = "Site IDs for the two vBond nodes."
  type        = list(number)
  default     = [120, 121]

  validation {
    condition     = length(var.vbond_site_ids) == 2
    error_message = "vbond_site_ids must contain exactly two values."
  }
}

variable "vsmart_site_ids" {
  description = "Site IDs for the two vSmart nodes."
  type        = list(number)
  default     = [130, 131]

  validation {
    condition     = length(var.vsmart_site_ids) == 2
    error_message = "vsmart_site_ids must contain exactly two values."
  }
}

variable "enabled_controller_keys" {
  description = "Optional subset of controller node keys to deploy. Set to null to deploy all controllers, [] to deploy none."
  type        = list(string)
  default     = null
  nullable    = true
}

variable "labels" {
  description = "Additional labels applied to supported STACKIT resources."
  type        = map(string)
  default     = {}
}

variable "custom_labels" {
  description = "Additional labels merged onto the appliance VM server resources on top of the mandatory Cisco SD-WAN exemption labels."
  type        = map(string)
  default     = {}
}

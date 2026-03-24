resource "stackit_network" "management" {
  project_id         = var.project_id
  name               = format("%s-sdwan-mgmt", var.prefix)
  dhcp               = true
  ipv4_gateway       = var.management_network_cidr == null ? null : cidrhost(var.management_network_cidr, 1)
  ipv4_nameservers   = var.network_ipv4_nameservers
  ipv4_prefix        = var.management_network_cidr
  ipv4_prefix_length = var.management_network_cidr == null ? var.management_network_prefix_length : null
  labels             = merge(var.labels, { role = "management" })
  routed             = true

}

resource "stackit_network" "transport" {
  project_id         = var.project_id
  name               = format("%s-sdwan-transport", var.prefix)
  dhcp               = true
  ipv4_gateway       = var.transport_network_cidr == null ? null : cidrhost(var.transport_network_cidr, 1)
  ipv4_nameservers   = var.network_ipv4_nameservers
  ipv4_prefix        = var.transport_network_cidr
  ipv4_prefix_length = var.transport_network_cidr == null ? var.transport_network_prefix_length : null
  labels             = merge(var.labels, { role = "transport" })
  routed             = true

}

resource "stackit_network" "cluster" {
  project_id         = var.project_id
  name               = format("%s-sdwan-cluster", var.prefix)
  dhcp               = false
  ipv4_gateway       = var.cluster_network_cidr == null ? null : cidrhost(var.cluster_network_cidr, 1)
  ipv4_nameservers   = var.network_ipv4_nameservers
  ipv4_prefix        = var.cluster_network_cidr
  ipv4_prefix_length = var.cluster_network_cidr == null ? var.cluster_network_prefix_length : null
  labels             = merge(var.labels, { role = "cluster" })
  routed             = true

}

resource "stackit_security_group" "management" {
  project_id  = var.project_id
  name        = format("%s-sdwan-management", var.prefix)
  description = "Ingress for management-plane admin access and DHCP-related traffic"
  labels      = merge(var.labels, { role = "management" })
  stateful    = true
}

resource "stackit_security_group_rule" "management_ingress_internal_tcp" {
  project_id        = var.project_id
  security_group_id = stackit_security_group.management.security_group_id
  direction         = "ingress"
  ether_type        = "IPv4"
  ip_range          = local.management_prefix
  protocol = {
    name = "tcp"
  }
}

resource "stackit_security_group_rule" "management_ingress_internal_udp" {
  project_id        = var.project_id
  security_group_id = stackit_security_group.management.security_group_id
  direction         = "ingress"
  ether_type        = "IPv4"
  ip_range          = local.management_prefix
  protocol = {
    name = "udp"
  }
}

resource "stackit_security_group_rule" "management_egress_tcp" {
  project_id        = var.project_id
  security_group_id = stackit_security_group.management.security_group_id
  direction         = "egress"
  ether_type        = "IPv4"
  ip_range          = "0.0.0.0/0"
  protocol = {
    name = "tcp"
  }
}

resource "stackit_security_group_rule" "management_egress_udp" {
  project_id        = var.project_id
  security_group_id = stackit_security_group.management.security_group_id
  direction         = "egress"
  ether_type        = "IPv4"
  ip_range          = "0.0.0.0/0"
  protocol = {
    name = "udp"
  }
}

resource "stackit_security_group_rule" "management_egress_icmp" {
  project_id        = var.project_id
  security_group_id = stackit_security_group.management.security_group_id
  direction         = "egress"
  ether_type        = "IPv4"
  ip_range          = "0.0.0.0/0"
  protocol = {
    name = "icmp"
  }
}

resource "stackit_security_group_rule" "management_admin_ingress" {
  for_each = {
    for entry in flatten([
      for cidr in local.admin_access_cidrs : [
        for port in local.admin_tcp_ports : {
          key  = format("%s-%d", replace(cidr, "/", "_"), port)
          cidr = cidr
          port = port
        }
      ]
    ]) : entry.key => entry
  }

  project_id        = var.project_id
  security_group_id = stackit_security_group.management.security_group_id
  direction         = "ingress"
  ether_type        = "IPv4"
  ip_range          = each.value.cidr
  port_range = {
    min = each.value.port
    max = each.value.port
  }
  protocol = {
    name = "tcp"
  }
}

resource "stackit_security_group_rule" "management_ssh_ingress" {
  for_each = {
    for cidr in local.ssh_access_cidrs : replace(cidr, "/", "_") => cidr
  }

  project_id        = var.project_id
  security_group_id = stackit_security_group.management.security_group_id
  direction         = "ingress"
  ether_type        = "IPv4"
  ip_range          = each.value
  port_range = {
    min = 22
    max = 22
  }
  protocol = {
    name = "tcp"
  }
}

resource "stackit_security_group_rule" "management_public_peer_ingress_tcp" {
  for_each = local.controller_public_peer_cidrs_by_name

  project_id        = var.project_id
  security_group_id = stackit_security_group.management.security_group_id
  direction         = "ingress"
  ether_type        = "IPv4"
  ip_range          = each.value
  protocol = {
    name = "tcp"
  }
}

resource "stackit_security_group_rule" "management_public_peer_ingress_udp" {
  for_each = local.controller_public_peer_cidrs_by_name

  project_id        = var.project_id
  security_group_id = stackit_security_group.management.security_group_id
  direction         = "ingress"
  ether_type        = "IPv4"
  ip_range          = each.value
  protocol = {
    name = "udp"
  }
}

resource "stackit_security_group_rule" "management_public_peer_ingress_icmp" {
  for_each = local.controller_public_peer_cidrs_by_name

  project_id        = var.project_id
  security_group_id = stackit_security_group.management.security_group_id
  direction         = "ingress"
  ether_type        = "IPv4"
  ip_range          = each.value
  protocol = {
    name = "icmp"
  }
}

resource "stackit_security_group" "transport" {
  project_id  = var.project_id
  name        = format("%s-sdwan-transport", var.prefix)
  description = "Ingress for SD-WAN transport and limited admin access"
  labels      = merge(var.labels, { role = "transport" })
  stateful    = true
}

resource "stackit_security_group" "cluster" {
  project_id  = var.project_id
  name        = format("%s-sdwan-cluster", var.prefix)
  description = "Ingress and egress for vManage cluster OOB east-west traffic"
  labels      = merge(var.labels, { role = "cluster" })
  stateful    = true
}

resource "stackit_security_group_rule" "cluster_ingress_tcp" {
  project_id        = var.project_id
  security_group_id = stackit_security_group.cluster.security_group_id
  direction         = "ingress"
  ether_type        = "IPv4"
  ip_range          = local.cluster_prefix
  protocol = {
    name = "tcp"
  }
}

resource "stackit_security_group_rule" "cluster_ingress_udp" {
  project_id        = var.project_id
  security_group_id = stackit_security_group.cluster.security_group_id
  direction         = "ingress"
  ether_type        = "IPv4"
  ip_range          = local.cluster_prefix
  protocol = {
    name = "udp"
  }
}

resource "stackit_security_group_rule" "cluster_ingress_icmp" {
  project_id        = var.project_id
  security_group_id = stackit_security_group.cluster.security_group_id
  direction         = "ingress"
  ether_type        = "IPv4"
  ip_range          = local.cluster_prefix
  protocol = {
    name = "icmp"
  }
}

resource "stackit_security_group_rule" "cluster_egress_tcp" {
  project_id        = var.project_id
  security_group_id = stackit_security_group.cluster.security_group_id
  direction         = "egress"
  ether_type        = "IPv4"
  ip_range          = local.cluster_prefix
  protocol = {
    name = "tcp"
  }
}

resource "stackit_security_group_rule" "cluster_egress_udp" {
  project_id        = var.project_id
  security_group_id = stackit_security_group.cluster.security_group_id
  direction         = "egress"
  ether_type        = "IPv4"
  ip_range          = local.cluster_prefix
  protocol = {
    name = "udp"
  }
}

resource "stackit_security_group_rule" "cluster_egress_icmp" {
  project_id        = var.project_id
  security_group_id = stackit_security_group.cluster.security_group_id
  direction         = "egress"
  ether_type        = "IPv4"
  ip_range          = local.cluster_prefix
  protocol = {
    name = "icmp"
  }
}

resource "stackit_security_group_rule" "transport_ingress_internal_tcp" {
  project_id        = var.project_id
  security_group_id = stackit_security_group.transport.security_group_id
  direction         = "ingress"
  ether_type        = "IPv4"
  ip_range          = local.transport_prefix
  protocol = {
    name = "tcp"
  }
}

resource "stackit_security_group_rule" "transport_ingress_internal_udp" {
  project_id        = var.project_id
  security_group_id = stackit_security_group.transport.security_group_id
  direction         = "ingress"
  ether_type        = "IPv4"
  ip_range          = local.transport_prefix
  protocol = {
    name = "udp"
  }
}

resource "stackit_security_group_rule" "transport_egress_tcp" {
  project_id        = var.project_id
  security_group_id = stackit_security_group.transport.security_group_id
  direction         = "egress"
  ether_type        = "IPv4"
  ip_range          = "0.0.0.0/0"
  protocol = {
    name = "tcp"
  }
}

resource "stackit_security_group_rule" "transport_egress_udp" {
  project_id        = var.project_id
  security_group_id = stackit_security_group.transport.security_group_id
  direction         = "egress"
  ether_type        = "IPv4"
  ip_range          = "0.0.0.0/0"
  protocol = {
    name = "udp"
  }
}

resource "stackit_security_group_rule" "transport_egress_icmp" {
  project_id        = var.project_id
  security_group_id = stackit_security_group.transport.security_group_id
  direction         = "egress"
  ether_type        = "IPv4"
  ip_range          = "0.0.0.0/0"
  protocol = {
    name = "icmp"
  }
}

resource "stackit_security_group_rule" "transport_admin_ingress" {
  for_each = {
    for entry in flatten([
      for cidr in local.admin_access_cidrs : [
        for port in local.admin_tcp_ports : {
          key  = format("%s-%d", replace(cidr, "/", "_"), port)
          cidr = cidr
          port = port
        }
      ]
    ]) : entry.key => entry
  }

  project_id        = var.project_id
  security_group_id = stackit_security_group.transport.security_group_id
  direction         = "ingress"
  ether_type        = "IPv4"
  ip_range          = each.value.cidr
  port_range = {
    min = each.value.port
    max = each.value.port
  }
  protocol = {
    name = "tcp"
  }
}

resource "stackit_security_group_rule" "transport_ssh_ingress" {
  for_each = {
    for cidr in local.ssh_access_cidrs : replace(cidr, "/", "_") => cidr
  }

  project_id        = var.project_id
  security_group_id = stackit_security_group.transport.security_group_id
  direction         = "ingress"
  ether_type        = "IPv4"
  ip_range          = each.value
  port_range = {
    min = 22
    max = 22
  }
  protocol = {
    name = "tcp"
  }
}

resource "stackit_security_group_rule" "transport_public_peer_ingress_tcp" {
  for_each = local.controller_public_peer_cidrs_by_name

  project_id        = var.project_id
  security_group_id = stackit_security_group.transport.security_group_id
  direction         = "ingress"
  ether_type        = "IPv4"
  ip_range          = each.value
  protocol = {
    name = "tcp"
  }
}

resource "stackit_security_group_rule" "transport_public_peer_ingress_udp" {
  for_each = local.controller_public_peer_cidrs_by_name

  project_id        = var.project_id
  security_group_id = stackit_security_group.transport.security_group_id
  direction         = "ingress"
  ether_type        = "IPv4"
  ip_range          = each.value
  protocol = {
    name = "udp"
  }
}

resource "stackit_security_group_rule" "transport_public_peer_ingress_icmp" {
  for_each = local.controller_public_peer_cidrs_by_name

  project_id        = var.project_id
  security_group_id = stackit_security_group.transport.security_group_id
  direction         = "ingress"
  ether_type        = "IPv4"
  ip_range          = each.value
  protocol = {
    name = "icmp"
  }
}

resource "stackit_security_group_rule" "transport_sdwan_control_ingress_tcp" {
  project_id        = var.project_id
  security_group_id = stackit_security_group.transport.security_group_id
  direction         = "ingress"
  ether_type        = "IPv4"
  ip_range          = "0.0.0.0/0"
  port_range = {
    min = var.vbond_port
    max = var.vbond_port
  }
  protocol = {
    name = "tcp"
  }
}

resource "stackit_security_group_rule" "transport_sdwan_control_ingress_udp" {
  project_id        = var.project_id
  security_group_id = stackit_security_group.transport.security_group_id
  direction         = "ingress"
  ether_type        = "IPv4"
  ip_range          = "0.0.0.0/0"
  port_range = {
    min = var.vbond_port
    max = var.vbond_port
  }
  protocol = {
    name = "udp"
  }
}

resource "stackit_network_interface" "controller_mgmt" {
  for_each = local.controller_nodes

  project_id         = var.project_id
  network_id         = stackit_network.management.network_id
  ipv4               = each.value.mgmt_ip
  labels             = merge(var.labels, { node = each.key, role = each.value.role, plane = "management" })
  name               = format("%s-mgmt", each.value.hostname)
  security_group_ids = [stackit_security_group.management.security_group_id]

  depends_on = [
    stackit_security_group_rule.management_admin_ingress,
    stackit_security_group_rule.management_egress_icmp,
    stackit_security_group_rule.management_egress_tcp,
    stackit_security_group_rule.management_egress_udp,
    stackit_security_group_rule.management_ingress_internal_tcp,
    stackit_security_group_rule.management_ingress_internal_udp,
    stackit_security_group_rule.management_ssh_ingress,
  ]
}

resource "stackit_network_interface" "controller_transport" {
  for_each = local.controller_nodes

  project_id         = var.project_id
  network_id         = stackit_network.transport.network_id
  ipv4               = each.value.transport_ip
  labels             = merge(var.labels, { node = each.key, role = each.value.role, plane = "transport" })
  name               = format("%s-transport", each.value.hostname)
  security           = true
  security_group_ids = [stackit_security_group.transport.security_group_id]

  depends_on = [
    stackit_security_group_rule.transport_admin_ingress,
    stackit_security_group_rule.transport_egress_icmp,
    stackit_security_group_rule.transport_egress_tcp,
    stackit_security_group_rule.transport_egress_udp,
    stackit_security_group_rule.transport_ingress_internal_tcp,
    stackit_security_group_rule.transport_ingress_internal_udp,
    stackit_security_group_rule.transport_sdwan_control_ingress_tcp,
    stackit_security_group_rule.transport_sdwan_control_ingress_udp,
    stackit_security_group_rule.transport_ssh_ingress,
  ]

  lifecycle {
    ignore_changes = [
      security_group_ids,
    ]
  }
}

resource "stackit_network_interface" "controller_cluster" {
  for_each = local.vmanage_nodes

  project_id         = var.project_id
  network_id         = stackit_network.cluster.network_id
  ipv4               = each.value.cluster_ip
  labels             = merge(var.labels, { node = each.key, role = each.value.role, plane = "cluster" })
  name               = format("%s-cluster", each.value.hostname)
  security           = true
  security_group_ids = [stackit_security_group.cluster.security_group_id]

  depends_on = [
    stackit_security_group_rule.cluster_ingress_icmp,
    stackit_security_group_rule.cluster_ingress_tcp,
    stackit_security_group_rule.cluster_ingress_udp,
    stackit_security_group_rule.cluster_egress_icmp,
    stackit_security_group_rule.cluster_egress_tcp,
    stackit_security_group_rule.cluster_egress_udp,
  ]
}

resource "stackit_public_ip" "controller_management" {
  for_each = var.management_public_ips_enabled ? local.controller_nodes : {}

  project_id           = var.project_id
  labels               = merge(var.labels, { node = each.key, role = each.value.role, plane = "management" })
  network_interface_id = stackit_network_interface.controller_mgmt[each.key].network_interface_id
  depends_on           = [stackit_server.controller]
}

resource "stackit_public_ip" "controller_transport" {
  for_each = var.transport_public_ips_enabled ? local.controller_nodes : {}

  project_id           = var.project_id
  labels               = merge(var.labels, { node = each.key, role = each.value.role, plane = "transport" })
  network_interface_id = stackit_network_interface.controller_transport[each.key].network_interface_id
  depends_on           = [stackit_server.controller]
}

resource "terraform_data" "vmanage_generated_certs" {
  count = local.vmanage_cert_mode_generated && length(local.vmanage_nodes) > 0 ? 1 : 0

  triggers_replace = [
    filesha256("${path.module}/scripts/generate_vmanage_example_certs.sh"),
    local.vmanage_generated_cert_dir_resolved,
    var.organization_name,
    local.vmanage_generated_root_common_name_effective,
    local.vmanage_generated_server_common_name_effective,
    tostring(var.vmanage_generated_cert_validity_days),
  ]

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-lc"]
    command = format(
      <<-EOT
      set -euo pipefail
      bash %q \
        --output-dir %q \
        --org %q \
        --root-cn %q \
        --server-cn %q \
        --valid-days %q
      EOT
      ,
      "${path.module}/scripts/generate_vmanage_example_certs.sh",
      local.vmanage_generated_cert_dir_resolved,
      var.organization_name,
      local.vmanage_generated_root_common_name_effective,
      local.vmanage_generated_server_common_name_effective,
      tostring(var.vmanage_generated_cert_validity_days),
    )
  }
}

data "local_file" "vmanage_generated_root_ca" {
  count = local.vmanage_cert_mode_generated && length(local.vmanage_nodes) > 0 ? 1 : 0

  filename   = local.vmanage_generated_root_ca_cert_path
  depends_on = [terraform_data.vmanage_generated_certs]
}

data "local_file" "vmanage_generated_server_cert" {
  count = local.vmanage_cert_mode_generated && length(local.vmanage_nodes) > 0 ? 1 : 0

  filename   = local.vmanage_generated_server_cert_path
  depends_on = [terraform_data.vmanage_generated_certs]
}

data "local_file" "vmanage_generated_server_key" {
  count = local.vmanage_cert_mode_generated && length(local.vmanage_nodes) > 0 ? 1 : 0

  filename   = local.vmanage_generated_server_key_path
  depends_on = [terraform_data.vmanage_generated_certs]
}

data "local_file" "vmanage_generated_server_csr" {
  count = local.vmanage_cert_mode_generated && length(local.vmanage_nodes) > 0 ? 1 : 0

  filename   = local.vmanage_generated_server_csr_path
  depends_on = [terraform_data.vmanage_generated_certs]
}

resource "stackit_server" "controller" {
  for_each = local.controller_nodes

  project_id        = var.project_id
  availability_zone = var.availability_zone
  desired_status    = each.value.personality == "vmanage" ? "inactive" : null
  labels            = merge(local.all_server_labels, { node = each.key, role = each.value.role })
  machine_type      = each.value.machine_type
  name              = each.value.hostname
  network_interfaces = concat(
    [
      stackit_network_interface.controller_mgmt[each.key].network_interface_id,
      stackit_network_interface.controller_transport[each.key].network_interface_id,
    ],
    contains(keys(local.vmanage_nodes), each.key) ? [stackit_network_interface.controller_cluster[each.key].network_interface_id] : []
  )
  user_data = each.value.personality == "vmanage" ? templatefile("${path.module}/cloud-init/vmanage-rootca.yaml.tftpl", {
    admin_password = var.admin_password
    hostname       = each.value.hostname
    root_ca_cert   = local.vmanage_root_ca_cert_content
    zcloud_xml = templatefile("${path.module}/cloud-init/vmanage.xml.tftpl", {
      admin_password_hash        = var.admin_password_hash
      certificate_installed      = false
      cluster_ip                 = each.value.cluster_ip
      cluster_prefix_length      = local.cluster_prefix_length
      domain_id                  = var.domain_id
      hostname                   = each.value.hostname
      organization_name          = var.organization_name
      rootcert_installed         = false
      site_id                    = each.value.site_id
      snmp_engine_id             = join(":", [for i in range(0, 24, 2) : substr(sha1(format("%s-%s", each.value.hostname, each.value.system_ip)), i, 2)])
      system_ip                  = each.value.system_ip
      vbond_hostname             = var.vbond_hostname
      vbond_ips                  = local.vbond_transport_ips
      vbond_port                 = var.vbond_port
      vmanage_signed_certificate = false
    })
    }) : each.value.personality == "vbond" ? templatefile("${path.module}/cloud-init/vbond-rootca.yaml.tftpl", {
    admin_password = var.admin_password
    root_ca_cert   = local.vbond_root_ca_cert_content
    ssh_public_key = var.ssh_public_key
    zcloud_xml = templatefile("${path.module}/cloud-init/vbond.xml.tftpl", {
      admin_password_hash = var.admin_password_hash
      domain_id           = var.domain_id
      hostname            = each.value.hostname
      organization_name   = var.organization_name
      site_id             = each.value.site_id
      system_ip           = each.value.system_ip
      transport_ip        = each.value.transport_ip
      vbond_port          = var.vbond_port
    })
    }) : templatefile("${path.module}/cloud-init/vsmart-rootca.yaml.tftpl", {
    admin_password = var.admin_password
    root_ca_cert   = local.vsmart_root_ca_cert_content
    ssh_public_key = var.ssh_public_key
    zcloud_xml = templatefile("${path.module}/cloud-init/vsmart.xml.tftpl", {
      admin_password_hash = var.admin_password_hash
      domain_id           = var.domain_id
      hostname            = each.value.hostname
      organization_name   = var.organization_name
      site_id             = each.value.site_id
      system_ip           = each.value.system_ip
      vbond_hostname      = var.vbond_hostname
      vbond_ips           = local.vbond_transport_ips
      vbond_port          = var.vbond_port
    })
  })

  boot_volume = each.value.personality == "vmanage" ? {
    performance_class     = null
    size                  = null
    source_id             = stackit_volume.vmanage_boot[each.key].volume_id
    source_type           = "volume"
    delete_on_termination = null
    } : {
    performance_class     = null
    size                  = each.value.boot_volume_size
    source_id             = each.value.image_id
    source_type           = "image"
    delete_on_termination = null
  }

  lifecycle {
    ignore_changes = [
      boot_volume,
      desired_status,
      network_interfaces,
    ]
  }
}

resource "stackit_volume" "vmanage_boot" {
  for_each = local.vmanage_storage_nodes

  project_id        = var.project_id
  availability_zone = var.availability_zone
  labels            = merge(var.labels, { node = each.key, role = each.value.role, plane = "boot" })
  name              = format("%s-boot", each.value.hostname)
  size              = each.value.boot_volume_size
  source = {
    id   = each.value.image_id
    type = "image"
  }
}

resource "stackit_volume" "vmanage_data" {
  for_each = local.vmanage_storage_nodes

  project_id        = var.project_id
  availability_zone = var.availability_zone
  labels            = merge(var.labels, { node = each.key, role = each.value.role, plane = "data" })
  name              = format("%s-data", each.value.hostname)
  performance_class = var.vmanage_data_volume_performance_class
  size              = var.vmanage_data_disk_size
}

resource "stackit_server_volume_attach" "vmanage_data" {
  for_each = local.vmanage_nodes

  project_id = var.project_id
  server_id  = stackit_server.controller[each.key].server_id
  volume_id  = stackit_volume.vmanage_data[each.key].volume_id
}

resource "terraform_data" "vmanage_transport_security_attach" {
  for_each = local.vmanage_nodes

  triggers_replace = [
    stackit_server.controller[each.key].server_id,
    stackit_network.transport.network_id,
    stackit_network_interface.controller_transport[each.key].network_interface_id,
    stackit_security_group.transport.security_group_id,
    try(stackit_public_ip.controller_transport[each.key].ip, ""),
  ]

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-lc"]
    command = format(
      <<-EOT
      set -euo pipefail
      for _ in $(seq 1 30); do
        if stackit network-interface update %s --project-id %s --region %s --network-id %s --security-groups %s -y; then
          exit 0
        fi
        sleep 10
      done
      echo "Timed out attaching transport security group to %s" >&2
      exit 1
      EOT
      ,
      stackit_network_interface.controller_transport[each.key].network_interface_id,
      var.project_id,
      var.region,
      stackit_network.transport.network_id,
      stackit_security_group.transport.security_group_id,
      stackit_network_interface.controller_transport[each.key].network_interface_id,
    )
  }
}

resource "terraform_data" "vmanage_start" {
  for_each = local.vmanage_nodes

  triggers_replace = [
    stackit_server.controller[each.key].server_id,
    stackit_server_volume_attach.vmanage_data[each.key].id,
    terraform_data.vmanage_transport_security_attach[each.key].id,
    stackit_network_interface.controller_transport[each.key].network_interface_id,
    try(stackit_public_ip.controller_management[each.key].ip, ""),
    try(stackit_public_ip.controller_transport[each.key].ip, ""),
    tostring(var.vmanage_prestart_settle_seconds),
  ]

  # vManage must see the data disk on its first real boot. The provider does
  # not expose inline extra-volume attachment on stackit_server, so we create
  # the server inactive, attach the data volume, then start it once.

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-lc"]
    command = format(
      <<-EOT
      set -euo pipefail
      sleep %s
      status="$(stackit server describe %s --project-id %s --region %s -o json | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))')"
      if [ "$status" != "ACTIVE" ]; then
        stackit server start %s --project-id %s --region %s -y
      fi
      for _ in $(seq 1 60); do
        status="$(stackit server describe %s --project-id %s --region %s -o json | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))')"
        if [ "$status" = "ACTIVE" ]; then
          exit 0
        fi
        sleep 5
      done
      echo "Timed out waiting for server %s to become ACTIVE" >&2
      exit 1
      EOT
      ,
      var.vmanage_prestart_settle_seconds,
      stackit_server.controller[each.key].server_id,
      var.project_id,
      var.region,
      stackit_server.controller[each.key].server_id,
      var.project_id,
      var.region,
      stackit_server.controller[each.key].server_id,
      var.project_id,
      var.region,
      stackit_server.controller[each.key].server_id,
    )
  }
}

resource "terraform_data" "vmanage_initialize" {
  for_each = var.run_vmanage_firstboot_init && (var.management_public_ips_enabled || var.transport_public_ips_enabled) ? local.vmanage_nodes : {}

  triggers_replace = [
    stackit_server.controller[each.key].server_id,
    stackit_server_volume_attach.vmanage_data[each.key].id,
    try(stackit_public_ip.controller_management[each.key].ip, ""),
    try(stackit_public_ip.controller_transport[each.key].ip, ""),
    filesha256("${path.module}/scripts/init_vmanage_firstboot.sh"),
  ]

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-lc"]
    command = format(
      <<-EOT
      set -euo pipefail
      bash %q %q %q admin %q %q
      EOT
      ,
      "${path.module}/scripts/init_vmanage_firstboot.sh",
      coalesce(try(stackit_public_ip.controller_management[each.key].ip, null), try(stackit_public_ip.controller_transport[each.key].ip, null)),
      var.admin_password,
      "vdb,sdb,xvdb,nvme1n1,hdc,sdc",
      coalesce(try(stackit_public_ip.controller_management[each.key].ip, null), try(stackit_public_ip.controller_transport[each.key].ip, null)),
    )
  }

  depends_on = [
    terraform_data.vmanage_start,
  ]
}

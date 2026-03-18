locals {
  management_prefix_length = var.management_network_cidr != null ? tonumber(split("/", var.management_network_cidr)[1]) : var.management_network_prefix_length
  transport_prefix_length  = var.transport_network_cidr != null ? tonumber(split("/", var.transport_network_cidr)[1]) : var.transport_network_prefix_length
  cluster_prefix_length    = var.cluster_network_cidr != null ? tonumber(split("/", var.cluster_network_cidr)[1]) : var.cluster_network_prefix_length
  service_prefix_length    = var.service_network_cidr != null ? tonumber(split("/", var.service_network_cidr)[1]) : var.service_network_prefix_length

  management_prefix = var.management_network_cidr != null ? var.management_network_cidr : stackit_network.management.ipv4_prefixes[0]
  transport_prefix  = var.transport_network_cidr != null ? var.transport_network_cidr : stackit_network.transport.ipv4_prefixes[0]
  cluster_prefix    = var.cluster_network_cidr != null ? var.cluster_network_cidr : stackit_network.cluster.ipv4_prefixes[0]
  service_prefix    = var.service_network_cidr != null ? var.service_network_cidr : stackit_network.service.ipv4_prefixes[0]

  management_gateway = stackit_network.management.ipv4_gateway
  transport_gateway  = stackit_network.transport.ipv4_gateway
  cluster_gateway    = stackit_network.cluster.ipv4_gateway
  service_gateway    = stackit_network.service.ipv4_gateway

  admin_access_cidrs  = distinct(var.admin_access_cidrs)
  ssh_access_cidrs    = distinct(length(var.ssh_access_cidrs) > 0 ? var.ssh_access_cidrs : var.admin_access_cidrs)
  admin_tcp_ports     = [22, 443, 8443]
  primary_vmanage_key = "vmanage01"

  vmanage_cert_mode_generated = var.vmanage_cert_mode == "generated"
  vmanage_cert_mode_provided  = var.vmanage_cert_mode == "provided"

  vmanage_generated_cert_dir_resolved = startswith(var.vmanage_generated_cert_dir, "/") ? var.vmanage_generated_cert_dir : "${path.module}/${var.vmanage_generated_cert_dir}"
  vmanage_generated_root_ca_key_path  = "${local.vmanage_generated_cert_dir_resolved}/root-ca.key"
  vmanage_generated_root_ca_cert_path = "${local.vmanage_generated_cert_dir_resolved}/root-ca.crt"
  vmanage_generated_server_key_path   = "${local.vmanage_generated_cert_dir_resolved}/server.key"
  vmanage_generated_server_csr_path   = "${local.vmanage_generated_cert_dir_resolved}/server.csr"
  vmanage_generated_server_cert_path  = "${local.vmanage_generated_cert_dir_resolved}/server.crt"

  vmanage_root_ca_cert_path_resolved = trimspace(var.vmanage_root_ca_cert_path) == "" ? "" : (
    startswith(var.vmanage_root_ca_cert_path, "/") ? var.vmanage_root_ca_cert_path : "${path.module}/${var.vmanage_root_ca_cert_path}"
  )
  vmanage_server_cert_path_resolved = trimspace(var.vmanage_server_cert_path) == "" ? "" : (
    startswith(var.vmanage_server_cert_path, "/") ? var.vmanage_server_cert_path : "${path.module}/${var.vmanage_server_cert_path}"
  )
  vmanage_server_key_path_resolved = trimspace(var.vmanage_server_key_path) == "" ? "" : (
    startswith(var.vmanage_server_key_path, "/") ? var.vmanage_server_key_path : "${path.module}/${var.vmanage_server_key_path}"
  )
  vmanage_server_csr_path_resolved = trimspace(var.vmanage_server_csr_path) == "" ? "" : (
    startswith(var.vmanage_server_csr_path, "/") ? var.vmanage_server_csr_path : "${path.module}/${var.vmanage_server_csr_path}"
  )
  vbond_root_ca_cert_path_resolved = trimspace(var.vbond_root_ca_cert_path) == "" ? "" : (
    startswith(var.vbond_root_ca_cert_path, "/") ? var.vbond_root_ca_cert_path : "${path.module}/${var.vbond_root_ca_cert_path}"
  )
  vbond_server_cert_path_resolved = trimspace(var.vbond_server_cert_path) == "" ? "" : (
    startswith(var.vbond_server_cert_path, "/") ? var.vbond_server_cert_path : "${path.module}/${var.vbond_server_cert_path}"
  )
  vbond_server_key_path_resolved = trimspace(var.vbond_server_key_path) == "" ? "" : (
    startswith(var.vbond_server_key_path, "/") ? var.vbond_server_key_path : "${path.module}/${var.vbond_server_key_path}"
  )
  vbond_server_csr_path_resolved = trimspace(var.vbond_server_csr_path) == "" ? "" : (
    startswith(var.vbond_server_csr_path, "/") ? var.vbond_server_csr_path : "${path.module}/${var.vbond_server_csr_path}"
  )
  vsmart_root_ca_cert_path_resolved = trimspace(var.vsmart_root_ca_cert_path) == "" ? "" : (
    startswith(var.vsmart_root_ca_cert_path, "/") ? var.vsmart_root_ca_cert_path : "${path.module}/${var.vsmart_root_ca_cert_path}"
  )
  vsmart_server_cert_path_resolved = trimspace(var.vsmart_server_cert_path) == "" ? "" : (
    startswith(var.vsmart_server_cert_path, "/") ? var.vsmart_server_cert_path : "${path.module}/${var.vsmart_server_cert_path}"
  )
  vsmart_server_key_path_resolved = trimspace(var.vsmart_server_key_path) == "" ? "" : (
    startswith(var.vsmart_server_key_path, "/") ? var.vsmart_server_key_path : "${path.module}/${var.vsmart_server_key_path}"
  )
  vsmart_server_csr_path_resolved = trimspace(var.vsmart_server_csr_path) == "" ? "" : (
    startswith(var.vsmart_server_csr_path, "/") ? var.vsmart_server_csr_path : "${path.module}/${var.vsmart_server_csr_path}"
  )
  c8000v_root_ca_cert_path_resolved = trimspace(var.c8000v_root_ca_cert_path) == "" ? "" : (
    startswith(var.c8000v_root_ca_cert_path, "/") ? var.c8000v_root_ca_cert_path : "${path.module}/${var.c8000v_root_ca_cert_path}"
  )
  c8000v_server_cert_path_resolved = trimspace(var.c8000v_server_cert_path) == "" ? "" : (
    startswith(var.c8000v_server_cert_path, "/") ? var.c8000v_server_cert_path : "${path.module}/${var.c8000v_server_cert_path}"
  )
  c8000v_server_key_path_resolved = trimspace(var.c8000v_server_key_path) == "" ? "" : (
    startswith(var.c8000v_server_key_path, "/") ? var.c8000v_server_key_path : "${path.module}/${var.c8000v_server_key_path}"
  )
  c8000v_server_csr_path_resolved = trimspace(var.c8000v_server_csr_path) == "" ? "" : (
    startswith(var.c8000v_server_csr_path, "/") ? var.c8000v_server_csr_path : "${path.module}/${var.c8000v_server_csr_path}"
  )

  vmanage_generated_root_common_name_effective   = trimspace(var.vmanage_generated_root_common_name) != "" ? trimspace(var.vmanage_generated_root_common_name) : format("%s Example Root CA", var.organization_name)
  vmanage_generated_server_common_name_effective = trimspace(var.vmanage_generated_server_common_name) != "" ? trimspace(var.vmanage_generated_server_common_name) : format("%s-vmanage.example", var.prefix)
  vmanage_symantec_root_ca_cert_path_resolved = trimspace(var.vmanage_symantec_root_ca_cert_path) == "" ? "" : (
    startswith(var.vmanage_symantec_root_ca_cert_path, "/") ? var.vmanage_symantec_root_ca_cert_path : "${path.module}/${var.vmanage_symantec_root_ca_cert_path}"
  )
  vmanage_symantec_root_ca_cert_content = local.vmanage_symantec_root_ca_cert_path_resolved != "" && fileexists(local.vmanage_symantec_root_ca_cert_path_resolved) ? trimspace(file(local.vmanage_symantec_root_ca_cert_path_resolved)) : ""
  vmanage_root_ca_cert_content = local.vmanage_cert_mode_generated ? trimspace(data.local_file.vmanage_generated_root_ca[0].content) : (
    local.vmanage_cert_mode_provided && local.vmanage_root_ca_cert_path_resolved != "" && fileexists(local.vmanage_root_ca_cert_path_resolved) ? trimspace(file(local.vmanage_root_ca_cert_path_resolved)) : ""
  )
  vmanage_server_cert_content = local.vmanage_cert_mode_generated ? trimspace(data.local_file.vmanage_generated_server_cert[0].content) : (
    local.vmanage_cert_mode_provided && local.vmanage_server_cert_path_resolved != "" && fileexists(local.vmanage_server_cert_path_resolved) ? trimspace(file(local.vmanage_server_cert_path_resolved)) : ""
  )
  vmanage_server_key_content = local.vmanage_cert_mode_generated ? trimspace(data.local_file.vmanage_generated_server_key[0].content) : (
    local.vmanage_cert_mode_provided && local.vmanage_server_key_path_resolved != "" && fileexists(local.vmanage_server_key_path_resolved) ? trimspace(file(local.vmanage_server_key_path_resolved)) : ""
  )
  vmanage_server_csr_content = local.vmanage_cert_mode_generated ? trimspace(data.local_file.vmanage_generated_server_csr[0].content) : (
    local.vmanage_cert_mode_provided && local.vmanage_server_csr_path_resolved != "" && fileexists(local.vmanage_server_csr_path_resolved) ? trimspace(file(local.vmanage_server_csr_path_resolved)) : ""
  )
  vbond_root_ca_cert_content  = local.vbond_root_ca_cert_path_resolved != "" && fileexists(local.vbond_root_ca_cert_path_resolved) ? trimspace(file(local.vbond_root_ca_cert_path_resolved)) : ""
  vbond_server_cert_content   = local.vbond_server_cert_path_resolved != "" && fileexists(local.vbond_server_cert_path_resolved) ? trimspace(file(local.vbond_server_cert_path_resolved)) : ""
  vbond_server_key_content    = local.vbond_server_key_path_resolved != "" && fileexists(local.vbond_server_key_path_resolved) ? trimspace(file(local.vbond_server_key_path_resolved)) : ""
  vbond_server_csr_content    = local.vbond_server_csr_path_resolved != "" && fileexists(local.vbond_server_csr_path_resolved) ? trimspace(file(local.vbond_server_csr_path_resolved)) : ""
  vsmart_root_ca_cert_content = local.vsmart_root_ca_cert_path_resolved != "" && fileexists(local.vsmart_root_ca_cert_path_resolved) ? trimspace(file(local.vsmart_root_ca_cert_path_resolved)) : ""
  vsmart_server_cert_content  = local.vsmart_server_cert_path_resolved != "" && fileexists(local.vsmart_server_cert_path_resolved) ? trimspace(file(local.vsmart_server_cert_path_resolved)) : ""
  vsmart_server_key_content   = local.vsmart_server_key_path_resolved != "" && fileexists(local.vsmart_server_key_path_resolved) ? trimspace(file(local.vsmart_server_key_path_resolved)) : ""
  vsmart_server_csr_content   = local.vsmart_server_csr_path_resolved != "" && fileexists(local.vsmart_server_csr_path_resolved) ? trimspace(file(local.vsmart_server_csr_path_resolved)) : ""
  c8000v_root_ca_cert_content = local.c8000v_root_ca_cert_path_resolved != "" && fileexists(local.c8000v_root_ca_cert_path_resolved) ? trimspace(file(local.c8000v_root_ca_cert_path_resolved)) : ""
  c8000v_server_cert_content  = local.c8000v_server_cert_path_resolved != "" && fileexists(local.c8000v_server_cert_path_resolved) ? trimspace(file(local.c8000v_server_cert_path_resolved)) : ""
  c8000v_server_key_content   = local.c8000v_server_key_path_resolved != "" && fileexists(local.c8000v_server_key_path_resolved) ? trimspace(file(local.c8000v_server_key_path_resolved)) : ""
  c8000v_server_csr_content   = local.c8000v_server_csr_path_resolved != "" && fileexists(local.c8000v_server_csr_path_resolved) ? trimspace(file(local.c8000v_server_csr_path_resolved)) : ""

  all_vmanage_storage_nodes = {
    for idx in range(3) :
    format("vmanage%02d", idx + 1) => {
      hostname         = format("%s-vmanage-%02d", var.prefix, idx + 1)
      role             = "vmanage"
      image_id         = var.image_ids.vmanage
      boot_volume_size = var.boot_volume_sizes.vmanage
    }
  }

  all_vmanage_nodes = {
    for idx in range(3) :
    format("vmanage%02d", idx + 1) => {
      hostname         = format("%s-vmanage-%02d", var.prefix, idx + 1)
      role             = "vmanage"
      personality      = "vmanage"
      image_id         = var.image_ids.vmanage
      machine_type     = var.machine_types.vmanage
      boot_volume_size = var.boot_volume_sizes.vmanage
      mgmt_ip          = cidrhost(local.management_prefix, 11 + idx)
      transport_ip     = cidrhost(local.transport_prefix, 11 + idx)
      cluster_ip       = cidrhost(local.cluster_prefix, 11 + idx)
      system_ip        = cidrhost(var.system_ip_cidr, 11 + idx)
      site_id          = var.vmanage_site_ids[idx]
    }
  }

  all_vbond_nodes = {
    for idx in range(2) :
    format("vbond%02d", idx + 1) => {
      hostname         = format("%s-vbond-%02d", var.prefix, idx + 1)
      role             = "vbond"
      personality      = "vbond"
      image_id         = var.image_ids.vbond
      machine_type     = var.machine_types.controller
      boot_volume_size = var.boot_volume_sizes.controller
      mgmt_ip          = cidrhost(local.management_prefix, 21 + idx)
      transport_ip     = cidrhost(local.transport_prefix, 21 + idx)
      system_ip        = cidrhost(var.system_ip_cidr, 21 + idx)
      site_id          = var.vbond_site_ids[idx]
    }
  }

  all_vsmart_nodes = {
    for idx in range(2) :
    format("vsmart%02d", idx + 1) => {
      hostname         = format("%s-vsmart-%02d", var.prefix, idx + 1)
      role             = "vsmart"
      personality      = "vsmart"
      image_id         = var.image_ids.vsmart
      machine_type     = var.machine_types.controller
      boot_volume_size = var.boot_volume_sizes.controller
      mgmt_ip          = cidrhost(local.management_prefix, 31 + idx)
      transport_ip     = cidrhost(local.transport_prefix, 31 + idx)
      system_ip        = cidrhost(var.system_ip_cidr, 31 + idx)
      site_id          = var.vsmart_site_ids[idx]
    }
  }

  vmanage_storage_nodes = {
    for key, value in local.all_vmanage_storage_nodes :
    key => value
    if var.enabled_controller_keys == null || contains(var.enabled_controller_keys, key)
  }

  vmanage_nodes = {
    for key, value in local.all_vmanage_nodes :
    key => value
    if var.enabled_controller_keys == null || contains(var.enabled_controller_keys, key)
  }

  vbond_nodes = {
    for key, value in local.all_vbond_nodes :
    key => value
    if var.enabled_controller_keys == null || contains(var.enabled_controller_keys, key)
  }

  vsmart_nodes = {
    for key, value in local.all_vsmart_nodes :
    key => value
    if var.enabled_controller_keys == null || contains(var.enabled_controller_keys, key)
  }

  controller_nodes = merge(local.vmanage_nodes, local.vbond_nodes, local.vsmart_nodes)

  all_edge_nodes = {
    for idx in range(2) :
    format("c8000v%02d", idx + 1) => {
      hostname         = format("%s-c8000v-%02d", var.prefix, idx + 1)
      role             = "c8000v"
      image_id         = var.image_ids.c8000v
      machine_type     = var.machine_types.c8000v
      boot_volume_size = var.boot_volume_sizes.c8000v
      mgmt_ip          = cidrhost(local.management_prefix, 101 + idx)
      transport_ip     = cidrhost(local.transport_prefix, 101 + idx)
      service_ip       = cidrhost(local.service_prefix, 101 + idx)
      system_ip        = cidrhost(var.system_ip_cidr, 101 + idx)
      site_id          = var.edge_site_ids[idx]
    }
  }

  edge_nodes = {
    for key, value in local.all_edge_nodes :
    key => value
    if var.enabled_edge_keys == null || contains(var.enabled_edge_keys, key)
  }

  primary_vbond_key          = "vbond01"
  primary_vbond_transport_ip = local.all_vbond_nodes[local.primary_vbond_key].transport_ip
  vbond_transport_ips        = [for key in sort(keys(local.all_vbond_nodes)) : local.all_vbond_nodes[key].transport_ip]
}

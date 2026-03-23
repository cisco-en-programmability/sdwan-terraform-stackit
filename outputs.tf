output "vmanage_urls" {
  description = "vManage management public IPs. Use these as the primary HTTPS entry points."
  value = {
    for key, node in local.vmanage_nodes :
    key => {
      hostname             = node.hostname
      management_ip        = node.mgmt_ip
      management_public_ip = try(stackit_public_ip.controller_management[key].ip, null)
      transport_ip         = node.transport_ip
      transport_public_ip  = try(stackit_public_ip.controller_transport[key].ip, null)
      cluster_ip           = node.cluster_ip
      url                  = try(format("https://%s", coalesce(try(stackit_public_ip.controller_management[key].ip, null), try(stackit_public_ip.controller_transport[key].ip, null))), null)
    }
  }
}

output "controller_inventory" {
  description = "Private and public addressing for controller nodes."
  value = {
    for key, node in local.controller_nodes :
    key => {
      hostname             = node.hostname
      role                 = node.role
      management_ip        = node.mgmt_ip
      management_public_ip = try(stackit_public_ip.controller_management[key].ip, null)
      transport_ip         = node.transport_ip
      transport_public_ip  = try(stackit_public_ip.controller_transport[key].ip, null)
      cluster_ip           = try(node.cluster_ip, null)
      system_ip            = node.system_ip
      site_id              = node.site_id
      server_id            = try(stackit_server.controller[key].server_id, null)
    }
  }
}

output "network_inventory" {
  description = "Resolved STACKIT network prefixes, gateways, nameservers, and routing properties."
  value = {
    management = {
      prefix      = local.management_prefix
      gateway     = stackit_network.management.ipv4_gateway
      nameservers = stackit_network.management.ipv4_nameservers
      public_ip   = stackit_network.management.public_ip
      routed      = stackit_network.management.routed
    }
    transport = {
      prefix      = local.transport_prefix
      gateway     = stackit_network.transport.ipv4_gateway
      nameservers = stackit_network.transport.ipv4_nameservers
      public_ip   = stackit_network.transport.public_ip
      routed      = stackit_network.transport.routed
    }
    cluster = {
      prefix      = local.cluster_prefix
      gateway     = stackit_network.cluster.ipv4_gateway
      nameservers = stackit_network.cluster.ipv4_nameservers
      public_ip   = try(stackit_network.cluster.public_ip, null)
      routed      = stackit_network.cluster.routed
    }
  }
}

output "primary_vbond_transport_ip" {
  description = "Private transport IP used by the day-0 bootstrap as the initial vBond target."
  value       = local.primary_vbond_transport_ip
}

output "certificate_flow_notice" {
  description = "Post-deploy certificate guidance for the currently selected controller certificate method."
  value = var.controller_certificate_method == "cisco_pki" ? format(
    "controller_certificate_method is cisco_pki. After terraform apply, data-disk formatting, and vManage cluster formation, run ./scripts/cert_api_script.py. Have a Cisco Smart Account username/password ready, and ensure the Cisco Smart Account organization matches organization_name=%s.",
    var.organization_name
  ) : "controller_certificate_method is enterprise_local. After terraform apply, data-disk formatting, and vManage cluster formation, run ./scripts/cert_api_script.py to complete the enterprise-local controller certificate flow."
}

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
    stackit = {
      source  = "stackitcloud/stackit"
      version = "~> 0.88.0"
    }
  }
}

provider "stackit" {
  default_region = var.region
}

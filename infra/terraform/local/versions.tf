# Validate-only Terraform foundation for the local Floci emulator (S2115 Task 6).
# Never applied against real AWS: the provider in provider.tf only accepts a
# loopback Floci endpoint and dummy credentials.
#
# Lock file decision: .terraform.lock.hcl is committed (HashiCorp's
# recommendation) so every machine resolves the same provider build; .terraform/
# and any state files are git-ignored.
terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Local state only; there is no remote backend for this stack.
  backend "local" {
    path = "terraform.tfstate"
  }
}

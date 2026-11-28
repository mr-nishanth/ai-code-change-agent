variable "floci_endpoint" {
  description = "Floci endpoint on the loopback interface. Any other value is rejected."
  type        = string
  default     = "http://127.0.0.1:4566"

  validation {
    condition     = can(regex("^http://(127\\.0\\.0\\.1|localhost):4566/?$", var.floci_endpoint))
    error_message = "floci_endpoint must be http://127.0.0.1:4566 or http://localhost:4566 (local Floci only)."
  }
}

# Dummy credentials and a loopback endpoint for every service this stack uses,
# per Floci docs/getting-started/terraform.md. No real AWS account is contacted.
provider "aws" {
  region     = "us-east-1"
  access_key = "test"
  secret_key = "test"

  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true
  s3_use_path_style           = true

  endpoints {
    dynamodb       = var.floci_endpoint
    s3             = var.floci_endpoint
    sqs            = var.floci_endpoint
    secretsmanager = var.floci_endpoint
    ecr            = var.floci_endpoint
    cloudwatchlogs = var.floci_endpoint
    cloudwatch     = var.floci_endpoint
    iam            = var.floci_endpoint
    sts            = var.floci_endpoint
  }
}

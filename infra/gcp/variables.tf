variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region for Cloud Run and Artifact Registry"
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Cloud Run service name (also used as Artifact Registry repo name)"
  type        = string
  default     = "mcp-relay"
}

variable "image" {
  description = "Full container image URL, e.g. us-central1-docker.pkg.dev/PROJECT/mcp-relay/mcp-relay:latest"
  type        = string
}

variable "cpu" {
  description = "vCPU allocation per instance (e.g. '1', '2')"
  type        = string
  default     = "1"
}

variable "memory" {
  description = "Memory allocation per instance (e.g. '512Mi', '1Gi')"
  type        = string
  default     = "512Mi"
}

variable "min_instances" {
  description = "Minimum number of Cloud Run instances (0 = scale to zero)"
  type        = number
  default     = 0
}

variable "max_instances" {
  description = "Maximum number of Cloud Run instances"
  type        = number
  default     = 10
}

variable "request_timeout_seconds" {
  description = "Maximum request duration before Cloud Run terminates the request"
  type        = number
  default     = 300
}

variable "allow_unauthenticated" {
  description = "Allow unauthenticated invocations. Set false for private/internal deployments."
  type        = bool
  default     = false
}

variable "secret_backend" {
  description = "Secret backend for mcp-relay (gcp | aws | azure | vault | env)"
  type        = string
  default     = "gcp"
}

variable "extra_env_vars" {
  description = "Additional environment variables to set on the Cloud Run service"
  type        = map(string)
  default     = {}
}

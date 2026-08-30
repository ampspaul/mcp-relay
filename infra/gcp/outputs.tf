output "service_url" {
  description = "HTTPS URL of the deployed mcp-relay Cloud Run service"
  value       = google_cloud_run_v2_service.mcp_relay.uri
}

output "service_name" {
  description = "Cloud Run service name"
  value       = google_cloud_run_v2_service.mcp_relay.name
}

output "service_account_email" {
  description = "Service account email used by mcp-relay (use this to grant per-secret IAM if needed)"
  value       = google_service_account.mcp_relay.email
}

output "artifact_registry_repo" {
  description = "Artifact Registry repository URL — push images here before deploying"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.mcp_relay.repository_id}"
}

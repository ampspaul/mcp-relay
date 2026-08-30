resource "google_service_account" "mcp_relay" {
  account_id   = "${var.service_name}-sa"
  display_name = "MCP Relay — Cloud Run service account"
  description  = "Least-privilege SA for mcp-relay: Secret Manager read + Cloud Run invoker."

  depends_on = [google_project_service.iam]
}

# Read secrets from Secret Manager (needed when SECRET_BACKEND=gcp)
resource "google_project_iam_member" "secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.mcp_relay.email}"
}

# Allow the SA to write structured logs to Cloud Logging
resource "google_project_iam_member" "log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.mcp_relay.email}"
}

resource "google_cloud_run_v2_service" "mcp_relay" {
  name     = var.service_name
  location = var.region

  template {
    service_account = google_service_account.mcp_relay.email

    timeout = "${var.request_timeout_seconds}s"

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    containers {
      image = var.image

      resources {
        limits = {
          cpu    = var.cpu
          memory = var.memory
        }
        # Release CPU between requests (cost saving for low-traffic deployments)
        cpu_idle = true
        # Allocate extra CPU during cold-start to reduce startup latency
        startup_cpu_boost = true
      }

      ports {
        name           = "http1"
        container_port = 8080
      }

      # Core relay configuration
      env {
        name  = "SECRET_BACKEND"
        value = var.secret_backend
      }

      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }

      # Structured JSON logging for Cloud Logging
      env {
        name  = "LOG_FORMAT"
        value = "json"
      }

      # Extra caller-supplied env vars (e.g. TOOL_REFRESH_INTERVAL_SECONDS)
      dynamic "env" {
        for_each = var.extra_env_vars
        content {
          name  = env.key
          value = env.value
        }
      }

      # Liveness — restart the container if /health stops responding
      liveness_probe {
        http_get {
          path = "/health"
        }
        initial_delay_seconds = 15
        period_seconds        = 30
        timeout_seconds       = 5
        failure_threshold     = 3
      }

      # Startup — give the relay time to connect to upstream MCP servers on boot
      startup_probe {
        http_get {
          path = "/health"
        }
        initial_delay_seconds = 5
        period_seconds        = 5
        timeout_seconds       = 5
        failure_threshold     = 12  # up to 60s for slow upstreams
      }
    }
  }

  depends_on = [google_project_service.run]
}

# Public access — only created when allow_unauthenticated = true
resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  count    = var.allow_unauthenticated ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.mcp_relay.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

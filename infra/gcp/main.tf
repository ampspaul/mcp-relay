terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }

  # Uncomment and fill in to store state in GCS:
  # backend "gcs" {
  #   bucket = "your-tf-state-bucket"
  #   prefix = "mcp-relay"
  # }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

resource "google_project_service" "run" {
  service            = "run.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "secretmanager" {
  service            = "secretmanager.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "artifactregistry" {
  service            = "artifactregistry.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "iam" {
  service            = "iam.googleapis.com"
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "mcp_relay" {
  repository_id = var.service_name
  location      = var.region
  format        = "DOCKER"
  description   = "Container images for mcp-relay"

  depends_on = [google_project_service.artifactregistry]
}

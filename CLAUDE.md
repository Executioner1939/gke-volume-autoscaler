# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is the Kubernetes Volume Autoscaler for Google Managed Prometheus (GMP) - a Python-based Kubernetes controller specifically designed for GKE Autopilot that automatically increases the size of Persistent Volume Claims (PVCs) when they are nearing full (either on space OR inode usage). It uses Google Managed Prometheus for metrics collection via the Google Cloud Monitoring API.

## Key Commands

### Development Commands

```bash
# Install dependencies
make deps
# or
pip3 install -r requirements.txt

# Run service locally in dry-run mode (safe for development)
make run
# This runs with: DRY_RUN=true VERBOSE=true python3 main.py

# Run service locally without dry-run (will actually scale volumes)
make run-hot

# Lint code using black formatter
make lint
# or
black .

# Run tests (currently not implemented)
make test-local
```

### Local Development Setup

To run locally against GKE with Google Managed Prometheus:
```bash
# First, ensure kubectl is configured to your GKE cluster
# Authenticate with Google Cloud
gcloud auth application-default login

# Set your project ID
export GCP_PROJECT_ID=your-project-id

# Run in dry-run mode
VERBOSE=true DRY_RUN=true python3 main.py
```

### Deployment Commands

```bash
# Add Helm repo
helm repo add devops-nirvana https://devops-nirvana.s3.amazonaws.com/helm-charts/

# Install with Helm on GKE
helm upgrade --install volume-autoscaler devops-nirvana/volume-autoscaler \
  --namespace YOUR_NAMESPACE \
  --create-namespace \
  --set gcp_project_id=$PROJECT_ID \
  --set serviceAccount.annotations."iam\.gke\.io/gcp-service-account"="volume-autoscaler@$PROJECT_ID.iam.gserviceaccount.com"
```

## Architecture

### Core Components

1. **main.py** - Main application loop that:
   - Queries Prometheus for disk usage metrics every `INTERVAL_TIME` seconds
   - Compares against PVCs in Kubernetes
   - Scales PVCs when thresholds are exceeded
   - Publishes metrics on port 8000
   - Handles graceful shutdown via signal handling

2. **helpers.py** - Contains all helper functions:
   - Google Managed Prometheus querying logic (`fetch_pvcs_from_gmp()`)
   - Kubernetes API interactions (`describe_all_pvcs()`, `scale_up_pvc()`)
   - Volume size calculations (`calculateBytesToScaleTo()`)
   - Configuration management and validation
   - Kubernetes event generation
   
3. **gmp_client.py** - Google Managed Prometheus client:
   - Handles authentication via Application Default Credentials
   - Queries Google Cloud Monitoring API for Prometheus metrics
   - Auto-detects GCP project ID from metadata service

3. **slack.py** - Slack integration for sending notifications when volumes are scaled

### Key Design Patterns

- **Google Managed Prometheus Integration**: Uses Google Cloud Monitoring API with PromQL queries to fetch disk usage metrics
- **Workload Identity Authentication**: Uses GKE Workload Identity for secure, credential-less authentication
- **Annotation-Based Configuration**: PVCs can override global settings via annotations (e.g., `volume.autoscaler.kubernetes.io/scale-above-percent`)
- **State Tracking**: Uses PVC annotations to track last resize time and enforce cooldown periods
- **Metrics Exposure**: Exposes Prometheus metrics on port 8000 for monitoring the autoscaler itself

### Important Environment Variables

The service is configured via environment variables (can be set via Helm values):
- `GCP_PROJECT_ID` - Google Cloud project ID (auto-detected in GKE)
- `DRY_RUN` - Set to "true" for testing without making changes
- `INTERVAL_TIME` - How often to check volumes (default: 60 seconds)
- `SCALE_ABOVE_PERCENT` - Threshold to trigger scaling (default: 80%)
- `SCALE_UP_PERCENT` - How much to increase volume size (default: 20%)
- `VERBOSE` - Enable detailed logging

## Testing & Validation

```bash
# Deploy test PVC that fills up quickly
kubectl apply -f examples/simple-pod-with-pvc.yaml

# Watch autoscaler logs
kubectl logs -f $(kubectl get pods | grep volume-autoscaler | awk '{print $1}')
```

## Important Notes

- Designed specifically for GKE Autopilot with Google Managed Prometheus
- Volumes must be actively mounted by a pod for metrics to be available
- Storage class must have `allowVolumeExpansion: true`
- Google Cloud has cooldown periods between volume resizes
- Supports both disk space and inode usage monitoring
- Requires Workload Identity for authentication
- Uses Google Cloud Monitoring API for querying metrics
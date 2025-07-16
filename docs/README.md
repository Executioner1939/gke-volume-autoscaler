# GKE Volume Autoscaler Helm Repository

This repository contains Helm charts for the GKE Volume Autoscaler.

## Usage

```bash
# Add the Helm repository
helm repo add gke-volume-autoscaler https://executioner1939.github.io/gke-volume-autoscaler/

# Update your local repository cache
helm repo update

# Install the chart
helm install volume-autoscaler gke-volume-autoscaler/volume-autoscaler \
  --namespace monitoring \
  --create-namespace \
  --set gcp_project_id=your-gcp-project-id \
  --set serviceAccount.annotations."iam\.gke\.io/gcp-service-account"="volume-autoscaler@your-project.iam.gserviceaccount.com"
```

## Available Charts

- **volume-autoscaler**: Kubernetes controller for automatic PVC scaling on GKE with Google Managed Prometheus

## Repository

- **Source Code**: https://github.com/Executioner1939/gke-volume-autoscaler
- **Issues**: https://github.com/Executioner1939/gke-volume-autoscaler/issues
- **Documentation**: https://github.com/Executioner1939/gke-volume-autoscaler/blob/master/README.md
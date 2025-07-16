# Kubernetes Volume Autoscaler for Google Managed Prometheus (GKE)

<a href="https://hub.docker.com/r/devopsnirvana/kubernetes-volume-autoscaler"><img src="https://img.shields.io/docker/pulls/devopsnirvana/kubernetes-volume-autoscaler?style=plastic" alt="Docker Hub Pulls"></a> <a href="https://github.com/DevOps-Nirvana/Kubernetes-Volume-Autoscaler/stargazers"><img src="https://img.shields.io/github/stars/DevOps-Nirvana/Kubernetes-Volume-Autoscaler?style=social" alt="Stargazers on Github"></a>

This repository contains a [Kubernetes controller](https://kubernetes.io/docs/concepts/architecture/controller/) that automatically increases the size of a Persistent Volume Claim (PVC) in Kubernetes when it is nearing full (either on space OR inode usage). It is specifically designed for Google Kubernetes Engine (GKE) Autopilot and uses Google Managed Prometheus for metrics.

Keeping your volumes at a minimal size can help reduce cost, but having to manually scale them up can be painful and a waste of time for a DevOps / Systems Administrator. This is often used on storage volumes against things in Kubernetes such as [Prometheus](https://prometheus.io), [MySQL](https://artifacthub.io/packages/helm/bitnami/mysql), [Redis](https://artifacthub.io/packages/helm/bitnami/redis), [RabbitMQ](https://bitnami.com/stack/rabbitmq/helm), or any other stateful service.

<img src="./.github/screenshot.resize.png" alt="Screenshot of usage">

## Requirements

- [GKE Autopilot Cluster](https://cloud.google.com/kubernetes-engine/docs/concepts/autopilot-overview) with Google Managed Prometheus enabled
- [kubectl binary](https://kubernetes.io/docs/tools/#kubectl) installed and setup with your cluster
- [The Helm 3.0+ binary](https://github.com/helm/helm/releases)
- [Google Managed Prometheus](https://cloud.google.com/stackdriver/docs/managed-prometheus) enabled on your cluster
- Using a Storage Class with `allowVolumeExpansion == true`
- Workload Identity enabled on your GKE cluster

## Prerequisites

### Storage Class Configuration

You must have a StorageClass which supports volume expansion. To check/enable this:

```bash
# First, check if your storage class supports volume expansion...
$ kubectl get storageclasses
NAME                 PROVISIONER             RECLAIMPOLICY   VOLUMEBINDINGMODE      ALLOWVOLUMEEXPANSION   AGE
standard-rwo         pd.csi.storage.gke.io   Delete          WaitForFirstConsumer   true                   10d

# If ALLOWVOLUMEEXPANSION is not set to true, patch it to enable this
kubectl patch storageclass standard-rwo -p '{"allowVolumeExpansion": true}'
```

### Google Cloud Service Account Setup

1. Create a GCP Service Account:
```bash
export PROJECT_ID=your-gcp-project-id
export NAMESPACE=your-namespace  # Where volume-autoscaler will be deployed

# Create the GCP service account
gcloud iam service-accounts create volume-autoscaler \
    --display-name="Volume Autoscaler Service Account" \
    --project=$PROJECT_ID

# Grant necessary permissions
gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:volume-autoscaler@$PROJECT_ID.iam.gserviceaccount.com" \
    --role="roles/monitoring.viewer"
```

2. Enable Workload Identity binding:
```bash
# Allow the Kubernetes service account to impersonate the GCP service account
gcloud iam service-accounts add-iam-policy-binding \
    volume-autoscaler@$PROJECT_ID.iam.gserviceaccount.com \
    --role roles/iam.workloadIdentityUser \
    --member "serviceAccount:$PROJECT_ID.svc.id.goog[$NAMESPACE/volume-autoscaler]"
```

## Installation with Helm

```bash
# Clone the repository
git clone https://github.com/Executioner1939/gke-volume-autoscaler.git
cd gke-volume-autoscaler

# Install directly from the local chart
helm install volume-autoscaler ./charts/volume-autoscaler \
  --namespace $NAMESPACE \
  --create-namespace \
  --set gcp_project_id=$PROJECT_ID \
  --set serviceAccount.annotations."iam\.gke\.io/gcp-service-account"="volume-autoscaler@$PROJECT_ID.iam.gserviceaccount.com"

# Or with Slack notifications
helm install volume-autoscaler ./charts/volume-autoscaler \
  --namespace $NAMESPACE \
  --create-namespace \
  --set gcp_project_id=$PROJECT_ID \
  --set serviceAccount.annotations."iam\.gke\.io/gcp-service-account"="volume-autoscaler@$PROJECT_ID.iam.gserviceaccount.com" \
  --set "slack_webhook_url=https://hooks.slack.com/services/123123123/4564564564/789789789789789789" \
  --set "slack_channel=my-slack-channel-name" \
  --set "slack_prefix=GKE Cluster: my-cluster"
```

### Advanced Helm usage

```bash
# To view what changes it will make (requires helm diff plugin)
helm diff upgrade volume-autoscaler ./charts/volume-autoscaler \
  --namespace $NAMESPACE \
  --set gcp_project_id=$PROJECT_ID \
  --set serviceAccount.annotations."iam\.gke\.io/gcp-service-account"="volume-autoscaler@$PROJECT_ID.iam.gserviceaccount.com"

# To remove the service
helm uninstall volume-autoscaler -n $NAMESPACE
```

## Validation

To confirm the volume autoscaler is working properly:

```bash
# Deploy a test PVC that fills up quickly
kubectl apply -f https://raw.githubusercontent.com/DevOps-Nirvana/Kubernetes-Volume-Autoscaler/master/examples/simple-pod-with-pvc.yaml

# Check the logs
kubectl logs -n $NAMESPACE -l app.kubernetes.io/name=volume-autoscaler --follow
```

## Usage Information and Nuances

### 1. Volume MUST be in use (pod is running with volume mounted)

For this to work, the volume must be mounted by a running pod. Google Managed Prometheus collects `kubelet_volume_stats_*` metrics only from mounted volumes.

### 2. Must have waited long enough since the last resize

Cloud providers restrict resize frequency. On Google Cloud, you must wait before resizing again. The default cooldown is configured at 6 hours plus 10 minutes buffer.

## Per-Volume Configuration via Annotations

Control behavior per-PVC with annotations:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: sample-volume-claim
  annotations:
    volume.autoscaler.kubernetes.io/scale-above-percent: "80"
    volume.autoscaler.kubernetes.io/scale-after-intervals: "5"
    volume.autoscaler.kubernetes.io/scale-up-percent: "20"
    volume.autoscaler.kubernetes.io/scale-up-min-increment: "1000000000"
    volume.autoscaler.kubernetes.io/scale-up-max-increment: "100000000000"
    volume.autoscaler.kubernetes.io/scale-up-max-size: "16000000000000"
    volume.autoscaler.kubernetes.io/scale-cooldown-time: "22200"
    volume.autoscaler.kubernetes.io/ignore: "false"
spec:
  accessModes:
  - ReadWriteOnce
  resources:
    requests:
      storage: 10Gi
  storageClassName: standard-rwo
```

## Prometheus Metrics

The autoscaler exposes metrics on port 8000:

| Metric Name                                | Type    | Description                                                        |
|--------------------------------------------|---------|--------------------------------------------------------------------|
| volume_autoscaler_resize_evaluated_total   | counter | Times we evaluated resizing PVCs                                   |
| volume_autoscaler_resize_attempted_total   | counter | Times we attempted to resize                                       |
| volume_autoscaler_resize_successful_total  | counter | Times we successfully resized                                      |
| volume_autoscaler_resize_failure_total     | counter | Times we failed to resize                                          |
| volume_autoscaler_num_valid_pvcs           | gauge   | Number of valid PVCs detected                                      |
| volume_autoscaler_num_pvcs_above_threshold | gauge   | Number of PVCs above the threshold                                 |
| volume_autoscaler_num_pvcs_below_threshold | gauge   | Number of PVCs below the threshold                                 |
| volume_autoscaler_release_info             | info    | Version information                                                |
| volume_autoscaler_settings_info            | info    | Current settings                                                   |

## Troubleshooting

### Check Workload Identity Setup

```bash
# Verify the annotation on the Kubernetes service account
kubectl get serviceaccount volume-autoscaler -n $NAMESPACE -o yaml

# Test authentication from a pod
kubectl run -it --rm debug \
  --image=google/cloud-sdk:slim \
  --serviceaccount=volume-autoscaler \
  -n $NAMESPACE \
  -- /bin/bash

# Inside the pod, check if authentication works
gcloud auth list
```

### Common Issues

1. **Authentication Errors**: Ensure Workload Identity is properly configured and the service accounts are correctly bound
2. **No Metrics Found**: Verify Google Managed Prometheus is enabled and collecting kubelet metrics
3. **Volumes Not Scaling**: Check that volumes are mounted and have exceeded the threshold for the required intervals

## Development

### Running Locally

```bash
# Install dependencies
pip3 install -r requirements.txt

# Set your GCP project
export GCP_PROJECT_ID=your-project-id

# Run in dry-run mode
DRY_RUN=true VERBOSE=true python3 main.py
```

### Environment Variables

| Variable Name          | Default        | Description |
|------------------------|----------------|-------------|
| GCP_PROJECT_ID         | auto-detect    | Google Cloud Project ID |
| INTERVAL_TIME          | 60             | How often to check volumes (seconds) |
| SCALE_ABOVE_PERCENT    | 80             | Threshold percentage to trigger scaling |
| SCALE_AFTER_INTERVALS  | 5              | Intervals above threshold before scaling |
| SCALE_UP_PERCENT       | 20             | Percentage to increase volume size |
| SCALE_UP_MIN_INCREMENT | 1000000000     | Minimum resize in bytes (1GB) |
| SCALE_UP_MAX_SIZE      | 16000000000000 | Maximum volume size in bytes (16TB) |
| SCALE_COOLDOWN_TIME    | 22200          | Cooldown between resizes (seconds) |
| DRY_RUN                | false          | Test mode - no actual resizing |
| VERBOSE                | false          | Enable detailed logging |

## Release History

### Current Release: 2.0.0-gmp
- Complete rewrite for Google Managed Prometheus
- Removed standard Prometheus support
- Native GKE Autopilot and Workload Identity integration
- Simplified configuration and deployment

### Previous Releases
See original repository for pre-GMP versions

## Contributors

This is a fork focused on Google Managed Prometheus. For the original multi-prometheus version, see the [original repository](https://github.com/DevOps-Nirvana/Kubernetes-Volume-Autoscaler).

## Repository

The source code for this project is available at: https://github.com/Executioner1939/gke-volume-autoscaler
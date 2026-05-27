# ExecRelay Helm Chart

Production-ready Helm chart for deploying ExecRelay to Kubernetes (AWS EKS or minikube).

## Chart Structure

```
execrelay/
├── Chart.yaml                 # Chart metadata
├── values.yaml               # Default production values
├── values-minikube.yaml      # Minikube-specific overrides
├── values-aws.yaml           # AWS EKS-specific overrides
└── templates/
    ├── deployment.yaml       # All 9 service deployments
    ├── service.yaml          # Service definitions
    ├── ingress.yaml          # Ingress routing
    ├── secret.yaml           # Sensitive credentials
    ├── hpa.yaml              # Horizontal Pod Autoscaler
    ├── servicemonitor.yaml   # Prometheus monitoring
    ├── serviceaccount.yaml   # RBAC service account
    └── _helpers.tpl          # Helm template helpers
```

## Quick Start - Minikube

### 1. Setup Minikube
```bash
minikube start --cpus=4 --memory=8192 --disk-size=50g
minikube addons enable ingress
minikube addons enable metrics-server  # For HPA
```

### 2. Setup Local Docker Registry (optional, for local image building)
```bash
eval $(minikube docker-env)
docker build -f apps/ingress/Dockerfile -t execrelay-ingress:latest .
docker build -f apps/bridge/Dockerfile -t execrelay-bridge:latest .
# ... build all 9 services
```

### 3. Create ConfigMap for NATS and PostgreSQL (if running locally)
```bash
# Start docker-compose for databases on host machine
docker compose up postgres nats redis -d

# Get host IP from minikube
HOST_IP=$(minikube ssh "route -n | grep ^ | awk '{ print $3; exit }'")

# Update values-minikube.yaml with HOST_IP
# natsUrl: "nats://execrelay:execrelay_nats_dev@$HOST_IP:4222"
# databaseUrl: "postgresql://execrelay:execrelay_dev_password@$HOST_IP:5432/execrelay"
```

### 4. Install the Chart
```bash
helm install execrelay ./infra/helm/execrelay \
  --namespace execrelay \
  --create-namespace \
  --values ./infra/helm/execrelay/values-minikube.yaml
```

### 5. Verify Deployment
```bash
kubectl get pods -n execrelay
kubectl get services -n execrelay
kubectl port-forward svc/execrelay-ingress 8081:8080 -n execrelay &
curl http://localhost:8081/health
```

### 6. Access Services
```bash
# Ingress webhook
kubectl port-forward svc/execrelay-ingress 8081:8080 -n execrelay

# Portal API
kubectl port-forward svc/execrelay-portal-api 8085:8080 -n execrelay

# Portal Web
kubectl port-forward svc/execrelay-portal-web 3001:80 -n execrelay
```

## Production Deployment - AWS EKS

### Prerequisites
- AWS EKS cluster running (1.27+)
- kubectl configured for EKS cluster
- Helm 3.x installed
- AWS RDS PostgreSQL instance
- AWS MQ for NATS
- IAM OIDC provider for cluster

### 1. Build and Push Images to ECR
```bash
AWS_ACCOUNT_ID=123456789012
AWS_REGION=us-east-1

# Create ECR repositories
for service in ingress bridge dxtrade persist portal-api tasks analytics reports portal-web; do
  aws ecr create-repository --repository-name execrelay-$service --region $AWS_REGION || true
done

# Login to ECR
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com

# Build and push each service
for service in ingress bridge dxtrade persist portal-api tasks analytics reports portal-web; do
  docker build -f apps/$service/Dockerfile -t execrelay-$service:latest .
  docker tag execrelay-$service:latest $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/execrelay-$service:latest
  docker push $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/execrelay-$service:latest
done
```

### 2. Create AWS RDS PostgreSQL
```bash
aws rds create-db-instance \
  --db-instance-identifier execrelay-db \
  --db-instance-class db.t3.small \
  --engine postgres \
  --master-username execrelay \
  --master-user-password 'ChangeMe!' \
  --allocated-storage 100 \
  --storage-type gp3 \
  --multi-az \
  --vpc-security-group-ids sg-xxxxxxxx \
  --db-subnet-group-name default
```

### 3. Create AWS MQ for NATS
```bash
aws mq create-broker \
  --broker-name execrelay-nats \
  --engine-type NATS \
  --engine-version 2.10 \
  --host-instance-type mq.t3.micro \
  --security-groups sg-xxxxxxxx
```

### 4. Create Kubernetes Secrets
```bash
kubectl create namespace execrelay

kubectl create secret docker-registry ecr-credentials \
  --docker-server=$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com \
  --docker-username=AWS \
  --docker-password=$(aws ecr get-login-password --region $AWS_REGION) \
  -n execrelay

kubectl create secret generic execrelay-secrets \
  --from-literal=nats-url="nats://user:password@execrelay-nats.us-east-1.mq.amazonaws.com:4222" \
  --from-literal=database-url="postgresql://execrelay:password@execrelay-rds.us-east-1.rds.amazonaws.com:5432/execrelay" \
  --from-literal=jwt-secret="$(openssl rand -hex 32)" \
  --from-literal=licenses="$(cat licenses.txt)" \
  --from-literal=pagerduty-key="$PAGERDUTY_KEY" \
  --from-literal=slack-webhook-url="$SLACK_WEBHOOK_URL" \
  -n execrelay
```

### 5. Install Chart on EKS
```bash
helm install execrelay ./infra/helm/execrelay \
  --namespace execrelay \
  --values ./infra/helm/execrelay/values-aws.yaml \
  --set image.registry=$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com
```

### 6. Setup Cert-Manager and TLS
```bash
# Install cert-manager
helm repo add jetstack https://charts.jetstack.io
helm install cert-manager jetstack/cert-manager \
  --namespace cert-manager \
  --create-namespace \
  --set installCRDs=true

# Create ClusterIssuer for Let's Encrypt
cat <<EOF | kubectl apply -f -
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: info@reycapitalsfo.com
    privateKeySecretRef:
      name: letsencrypt-prod
    solvers:
    - http01:
        ingress:
          class: aws-load-balancer
EOF
```

### 7. Configure DNS
Point the following CNAMEs to the ALB endpoint:
- `api.execrelay.com` → ALB DNS name
- `webhooks.execrelay.com` → ALB DNS name  
- `portal.execrelay.com` → ALB DNS name

### 8. Verify Deployment
```bash
kubectl get pods -n execrelay
kubectl get ingress -n execrelay
kubectl describe ingress execrelay -n execrelay
```

## Configuration

### Key Values to Override

```yaml
# Image registry (required for production)
image:
  registry: 123456789012.dkr.ecr.us-east-1.amazonaws.com

# Service replicas
services:
  ingress:
    replicaCount: 3
  bridge:
    replicaCount: 2

# Autoscaling
autoscaling:
  enabled: true
  minReplicas: 2
  maxReplicas: 10
  targetCPUUtilizationPercentage: 70

# Database and NATS URLs (from secrets)
natsUrl: "nats://user:password@broker:4222"
databaseUrl: "postgresql://user:password@postgres:5432/execrelay"

# Ingress configuration
ingress:
  enabled: true
  hosts:
    - host: api.execrelay.com
      paths:
        - path: /
          pathType: Prefix
```

## Monitoring

ServiceMonitor resources are automatically created for Prometheus Operator integration.

View metrics in Prometheus:
```
up{job="execrelay-ingress"}
rate(ingress_webhook_requests_total[5m])
histogram_quantile(0.99, rate(ingress_webhook_duration_seconds_bucket[5m]))
```

## Upgrade Strategy

```bash
# Update service images
helm upgrade execrelay ./infra/helm/execrelay \
  --namespace execrelay \
  --values ./infra/helm/execrelay/values-aws.yaml \
  --set services.ingress.image.tag=v1.0.1

# Rollback if needed
helm rollback execrelay 1 -n execrelay
```

## Troubleshooting

### Pods not starting
```bash
kubectl describe pod -n execrelay execrelay-ingress-0
kubectl logs -n execrelay execrelay-ingress-0
```

### Image pull errors
```bash
kubectl describe pod -n execrelay execrelay-ingress-0 | grep -A5 "Events"
# Verify ECR credentials: kubectl get secret ecr-credentials -n execrelay -o yaml
```

### Network connectivity
```bash
# Test pod-to-pod connectivity
kubectl run -it --rm debug --image=busybox -n execrelay -- sh
wget http://execrelay-postgres:5432  # should hang, postgres doesn't speak HTTP
```

## Resource Requirements

| Service | CPU Request | CPU Limit | Memory Request | Memory Limit |
|---------|-------------|-----------|----------------|--------------|
| ingress | 100m        | 500m      | 128Mi          | 512Mi        |
| bridge  | 100m        | 500m      | 128Mi          | 512Mi        |
| dxtrade | 100m        | 500m      | 128Mi          | 512Mi        |
| persist | 100m        | 500m      | 128Mi          | 512Mi        |
| portal-api | 100m     | 500m      | 128Mi          | 512Mi        |
| tasks   | 100m        | 500m      | 128Mi          | 512Mi        |
| analytics | 100m      | 500m      | 128Mi          | 512Mi        |
| reports | 100m        | 500m      | 128Mi          | 512Mi        |
| portal-web | 50m      | 200m      | 64Mi           | 256Mi        |

**Total minimum cluster:** 4 CPU, 4 GB RAM (for 3x ingress, 2x bridge/dxtrade/persist, 1x others)

## Support

For issues or questions, contact the development team.

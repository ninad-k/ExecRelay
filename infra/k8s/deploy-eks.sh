#!/bin/bash

# Deploy ExecRelay to AWS EKS
# Requires: AWS CLI, kubectl, helm, ECR images pushed, RDS/MQ provisioned

set -e

NAMESPACE="execrelay"
CLUSTER_NAME=${CLUSTER_NAME:-execrelay-cluster}
REGION=${AWS_REGION:-us-east-1}
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_REGISTRY="$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com"
CHART_PATH="./infra/helm/execrelay"

echo "======================================="
echo "ExecRelay AWS EKS Deployment"
echo "======================================="
echo ""
echo "Cluster: $CLUSTER_NAME"
echo "Region: $REGION"
echo "Account: $ACCOUNT_ID"
echo ""

# 1. Check prerequisites
echo "[1/5] Checking prerequisites..."
command -v aws &> /dev/null || { echo "❌ AWS CLI not found"; exit 1; }
command -v kubectl &> /dev/null || { echo "❌ kubectl not found"; exit 1; }
command -v helm &> /dev/null || { echo "❌ helm not found"; exit 1; }
echo "✓ All tools installed"

# 2. Configure kubectl for EKS
echo ""
echo "[2/5] Configuring kubectl..."
aws eks update-kubeconfig \
  --name $CLUSTER_NAME \
  --region $REGION > /dev/null
echo "✓ kubectl configured for EKS cluster"

# 3. Create namespace and ECR credentials
echo ""
echo "[3/5] Setting up Kubernetes namespace and secrets..."
kubectl create namespace $NAMESPACE --dry-run=client -o yaml | kubectl apply -f - > /dev/null

# Create ECR pull secret
echo "   Creating ECR pull secret..."
aws ecr get-login-password --region $REGION | \
  kubectl create secret docker-registry ecr-credentials \
    --docker-server=$ECR_REGISTRY \
    --docker-username=AWS \
    --docker-password-stdin \
    -n $NAMESPACE \
    --dry-run=client -o yaml | kubectl apply -f - > /dev/null

# Create app secrets (these should be updated with real values)
echo "   Creating application secrets..."
kubectl create secret generic execrelay-secrets \
  --from-literal=nats-url="$NATS_URL" \
  --from-literal=database-url="$DATABASE_URL" \
  --from-literal=jwt-secret="$(openssl rand -hex 32)" \
  --from-literal=licenses="$EXECRELAY_LICENSES" \
  --from-literal=pagerduty-key="$PAGERDUTY_KEY" \
  --from-literal=slack-webhook-url="$SLACK_WEBHOOK_URL" \
  -n $NAMESPACE \
  --dry-run=client -o yaml | kubectl apply -f - > /dev/null

echo "✓ Namespace and secrets configured"

# 4. Create values file with AWS-specific settings
echo ""
echo "[4/5] Deploying Helm chart to EKS..."
TEMP_VALUES=$(mktemp)
cat > "$TEMP_VALUES" <<EOF
image:
  registry: $ECR_REGISTRY

imagePullSecrets:
  - name: ecr-credentials

services:
  ingress:
    service:
      type: LoadBalancer

natsUrl: "$NATS_URL"
databaseUrl: "$DATABASE_URL"
jwtSecret: "$(openssl rand -hex 32)"
licenses: "$EXECRELAY_LICENSES"
pagerdutyKey: "$PAGERDUTY_KEY"
slackWebhookUrl: "$SLACK_WEBHOOK_URL"

autoscaling:
  enabled: true
  minReplicas: 2
  maxReplicas: 10

ingress:
  enabled: true
  className: aws-load-balancer
  annotations:
    alb.ingress.kubernetes.io/scheme: internet-facing
    alb.ingress.kubernetes.io/target-type: ip
    cert-manager.io/cluster-issuer: letsencrypt-prod
EOF

# Deploy chart
if helm upgrade --install execrelay "$CHART_PATH" \
  --namespace $NAMESPACE \
  --values $TEMP_VALUES \
  --wait \
  --timeout 10m; then
  echo "✓ Helm chart deployed"
else
  echo "❌ Helm deployment failed"
  rm -f "$TEMP_VALUES"
  exit 1
fi
rm -f "$TEMP_VALUES"

# 5. Wait for LoadBalancer and show access info
echo ""
echo "[5/5] Waiting for LoadBalancer..."
echo "   This may take 2-3 minutes..."

INGRESS_HOSTNAME=""
TIMEOUT=300
ELAPSED=0
while [ -z "$INGRESS_HOSTNAME" ] && [ $ELAPSED -lt $TIMEOUT ]; do
  INGRESS_HOSTNAME=$(kubectl get svc execrelay-ingress \
    -n $NAMESPACE \
    -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null)
  if [ -z "$INGRESS_HOSTNAME" ]; then
    sleep 10
    ELAPSED=$((ELAPSED + 10))
  fi
done

if [ -z "$INGRESS_HOSTNAME" ]; then
  echo "⚠ LoadBalancer not ready yet. Check status with:"
  echo "   kubectl get svc execrelay-ingress -n $NAMESPACE"
else
  echo "✓ LoadBalancer ready"
fi

echo ""
echo "======================================="
echo "✓ ExecRelay deployed to EKS!"
echo "======================================="
echo ""
echo "Access Information:"
echo ""
if [ -n "$INGRESS_HOSTNAME" ]; then
  echo "Ingress URL: http://$INGRESS_HOSTNAME"
  echo "Health check: curl http://$INGRESS_HOSTNAME/health"
  echo ""
  echo "Update your DNS records to point to:"
  echo "  api.execrelay.com -> $INGRESS_HOSTNAME"
  echo "  webhooks.execrelay.com -> $INGRESS_HOSTNAME"
  echo "  portal.execrelay.com -> $INGRESS_HOSTNAME"
else
  echo "Get LoadBalancer hostname:"
  echo "   kubectl get svc execrelay-ingress -n $NAMESPACE"
fi

echo ""
echo "Useful commands:"
echo ""
echo "View pod status:"
echo "   kubectl get pods -n $NAMESPACE"
echo ""
echo "View service endpoints:"
echo "   kubectl get svc -n $NAMESPACE"
echo ""
echo "View logs:"
echo "   kubectl logs -n $NAMESPACE -f -l app.kubernetes.io/name=execrelay"
echo ""
echo "Monitor deployment:"
echo "   kubectl get events -n $NAMESPACE --watch"
echo ""

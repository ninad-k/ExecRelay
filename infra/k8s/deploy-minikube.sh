#!/bin/bash

# Deploy ExecRelay to minikube
# Requires: minikube started, kubectl configured, docker-compose running for databases

set -e

NAMESPACE="execrelay"
CHART_PATH="./infra/helm/execrelay"
VALUES_FILE="$CHART_PATH/values-minikube.yaml"

echo "======================================="
echo "ExecRelay Minikube Deployment"
echo "======================================="
echo ""

# 1. Check prerequisites
echo "[1/6] Checking prerequisites..."
command -v minikube &> /dev/null || { echo "❌ minikube not found"; exit 1; }
command -v kubectl &> /dev/null || { echo "❌ kubectl not found"; exit 1; }
command -v docker &> /dev/null || { echo "❌ docker not found"; exit 1; }

# Check minikube status
if ! minikube status | grep -q "Running"; then
  echo "❌ minikube is not running"
  echo "   Start it with: minikube start --cpus=4 --memory=8192 --disk-size=50g"
  exit 1
fi
echo "✓ minikube is running"

# Check docker-compose stack
if ! docker compose ps | grep -q "postgres"; then
  echo "❌ docker-compose postgres not running"
  echo "   Start it with: docker compose up postgres nats redis -d"
  exit 1
fi
echo "✓ docker-compose services running"

# 2. Build images in minikube
echo ""
echo "[2/6] Building Docker images in minikube..."
eval $(minikube docker-env)

SERVICES=("ingress" "bridge" "dxtrade" "persist" "portal-api" "tasks" "analytics" "reports" "portal-web")
for service in "${SERVICES[@]}"; do
  echo "   Building execrelay-$service..."
  docker build \
    -f apps/$service/Dockerfile \
    -t execrelay-$service:latest \
    . > /dev/null 2>&1
done
echo "✓ All images built"

# 3. Create namespace
echo ""
echo "[3/6] Creating Kubernetes namespace..."
kubectl create namespace $NAMESPACE --dry-run=client -o yaml | kubectl apply -f - > /dev/null
echo "✓ Namespace '$NAMESPACE' ready"

# 4. Get host IP for database connections
echo ""
echo "[4/6] Configuring database access..."
HOST_IP=$(minikube ssh "route -n | grep ^ | awk '{ print \$3; exit }'")
echo "   Host IP (from minikube): $HOST_IP"

# Update values file with host IP
TEMP_VALUES=$(mktemp)
sed "s|postgres:5432|$HOST_IP:5432|g; s|nats:4222|$HOST_IP:4222|g" "$VALUES_FILE" > "$TEMP_VALUES"
echo "✓ Updated database connection URLs"

# 5. Deploy Helm chart
echo ""
echo "[5/6] Deploying Helm chart..."
if helm template execrelay "$CHART_PATH" -f "$TEMP_VALUES" | kubectl apply -f - -n $NAMESPACE; then
  echo "✓ Helm chart deployed"
else
  echo "❌ Helm chart deployment failed"
  rm -f "$TEMP_VALUES"
  exit 1
fi
rm -f "$TEMP_VALUES"

# 6. Wait for rollout
echo ""
echo "[6/6] Waiting for deployments to be ready..."
echo "   This may take 1-2 minutes..."

SERVICES_TO_WAIT=("ingress" "bridge" "persist" "portal-api")
for service in "${SERVICES_TO_WAIT[@]}"; do
  echo "   Waiting for execrelay-$service..."
  kubectl rollout status deployment/execrelay-$service -n $NAMESPACE --timeout=5m > /dev/null 2>&1 || {
    echo "❌ Deployment failed for execrelay-$service"
    echo "   Check logs with: kubectl logs -n $NAMESPACE -l app.kubernetes.io/component=$service"
    exit 1
  }
done
echo "✓ All deployments ready"

echo ""
echo "======================================="
echo "✓ ExecRelay deployed to minikube!"
echo "======================================="
echo ""
echo "Next steps:"
echo ""
echo "1. Port-forward to access services:"
echo "   kubectl port-forward svc/execrelay-ingress 8081:8080 -n $NAMESPACE &"
echo "   kubectl port-forward svc/execrelay-portal-api 8085:8080 -n $NAMESPACE &"
echo "   kubectl port-forward svc/execrelay-portal-web 3001:80 -n $NAMESPACE &"
echo ""
echo "2. Test ingress endpoint:"
echo "   curl http://localhost:8081/health"
echo ""
echo "3. View pod status:"
echo "   kubectl get pods -n $NAMESPACE"
echo ""
echo "4. View logs:"
echo "   kubectl logs -n $NAMESPACE -f -l app.kubernetes.io/name=execrelay"
echo ""
echo "5. Run load tests:"
echo "   make loadtest RATE=100"
echo ""

# AWS Infrastructure Setup Guide

Complete guide for provisioning AWS infrastructure for ExecRelay production deployment.

## Prerequisites

- AWS Account with appropriate IAM permissions
- AWS CLI v2 configured with credentials
- VPC and subnets created (or use default)
- EKS cluster running (1.27+)
- kubectl and helm configured

## 1. Create RDS PostgreSQL Database

### Using AWS Console

1. Go to RDS → Create Database
2. Engine: PostgreSQL 14.x or later
3. DB instance identifier: `execrelay-db`
4. Master username: `execrelay`
5. Auto-generate password (save it)
6. Connectivity:
   - VPC: Same as EKS cluster
   - DB subnet group: Create new or use default
   - Public accessibility: No
   - VPC security group: Create new → Allow ingress from EKS cluster on port 5432
7. Database options:
   - Database name: `execrelay`
   - Enable automated backups: Yes (7 days)
   - Backup window: 02:00-03:00 UTC
8. Enable encryption at rest: Yes
9. Storage: gp3, 100 GB with autoscaling enabled
10. Multi-AZ: Yes

### Using AWS CLI

```bash
#!/bin/bash
# setup-rds.sh

DB_IDENTIFIER="execrelay-db"
DB_PASSWORD=$(openssl rand -base64 24)
DB_SUBNET_GROUP="default"
SECURITY_GROUP_ID="sg-xxxxxxxx"  # EKS cluster security group

aws rds create-db-instance \
  --db-instance-identifier $DB_IDENTIFIER \
  --db-instance-class db.t3.small \
  --engine postgres \
  --engine-version 14.10 \
  --master-username execrelay \
  --master-user-password "$DB_PASSWORD" \
  --allocated-storage 100 \
  --storage-type gp3 \
  --storage-encrypted \
  --enable-iam-database-authentication \
  --vpc-security-group-ids $SECURITY_GROUP_ID \
  --db-subnet-group-name $DB_SUBNET_GROUP \
  --publicly-accessible false \
  --multi-az \
  --backup-retention-period 7 \
  --preferred-backup-window "02:00-03:00" \
  --preferred-maintenance-window "sun:03:00-sun:04:00" \
  --enable-cloudwatch-logs-exports postgresql \
  --deletion-protection \
  --region us-east-1

# Wait for database to be available
aws rds wait db-instance-available \
  --db-instance-identifier $DB_IDENTIFIER \
  --region us-east-1

# Get endpoint
ENDPOINT=$(aws rds describe-db-instances \
  --db-instance-identifier $DB_IDENTIFIER \
  --query 'DBInstances[0].Endpoint.Address' \
  --output text \
  --region us-east-1)

echo "Database created successfully"
echo "Endpoint: $ENDPOINT"
echo "Password: $DB_PASSWORD"
echo "Store these securely!"
```

### Post-Creation

```bash
# Enable TimescaleDB extension
PGPASSWORD=$DB_PASSWORD psql \
  -h $ENDPOINT \
  -U execrelay \
  -d execrelay \
  -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"

# Initialize schema
PGPASSWORD=$DB_PASSWORD psql \
  -h $ENDPOINT \
  -U execrelay \
  -d execrelay \
  -f infra/docker/postgres/init/001_schema.sql
```

## 2. Create AWS MQ for NATS

### Using AWS Console

1. Go to MQ → Create broker
2. Broker engine: NATS
3. Broker name: `execrelay-nats`
4. Deployment mode: Single-instance (or High-availability cluster for production)
5. Broker instance type: `mq.t3.micro`
6. Storage volume: 10 GB
7. Network:
   - VPC: Same as EKS cluster
   - Subnet: Select private subnet
   - Security group: Create new → Allow ingress from EKS cluster on 4222
8. User management: Create admin user
9. Logging: Enable CloudWatch logs

### Using AWS CLI

```bash
#!/bin/bash
# setup-mq.sh

BROKER_NAME="execrelay-nats"
BROKER_PASSWORD=$(openssl rand -base64 24)
SUBNET_IDS="subnet-xxxxxxxx"
SECURITY_GROUP_ID="sg-xxxxxxxx"

aws mq create-broker \
  --broker-name $BROKER_NAME \
  --engine-type NATS \
  --engine-version 2.10 \
  --host-instance-type mq.t3.micro \
  --users 'Username=execrelay,Password='$BROKER_PASSWORD',ConsoleAccess=true' \
  --security-groups $SECURITY_GROUP_ID \
  --subnet-ids $SUBNET_IDS \
  --storage-type EBS \
  --auto-minor-version-upgrade true \
  --logs BrokerLogs={CloudWatch={Enabled=true,LogGroup=/aws/mq/execrelay}} \
  --region us-east-1

# Wait for broker to be available
aws mq wait broker-created \
  --broker-id $BROKER_NAME \
  --region us-east-1 2>/dev/null || sleep 60

# Get broker information
BROKER_ENDPOINT=$(aws mq describe-brokers \
  --region us-east-1 \
  --query "BrokerSummaries[?BrokerName=='$BROKER_NAME'].BrokerArn" \
  --output text)

echo "Broker created successfully"
echo "Broker ARN: $BROKER_ENDPOINT"
echo "Username: execrelay"
echo "Password: $BROKER_PASSWORD"
echo "Store these securely!"
```

## 3. Setup Security Groups

### EKS Cluster Security Group

Allow inbound traffic:
- Port 5432 (PostgreSQL) from EKS cluster
- Port 4222 (NATS) from EKS cluster

### RDS Security Group

Allow inbound traffic:
- Port 5432 (PostgreSQL) from EKS security group
- Port 5432 (PostgreSQL) from your local IP (for migrations)

### MQ Security Group

Allow inbound traffic:
- Port 4222 (NATS) from EKS security group

## 4. Create Kubernetes Secrets

```bash
#!/bin/bash

NAMESPACE="execrelay"
DB_ENDPOINT="execrelay-db.c0123456789.us-east-1.rds.amazonaws.com"
DB_PASSWORD="your-db-password"
MQ_ENDPOINT="execrelay-nats.xxxxx.mq.us-east-1.amazonaws.com"
MQ_PASSWORD="your-mq-password"

# Create namespace
kubectl create namespace $NAMESPACE --dry-run=client -o yaml | kubectl apply -f -

# Create secrets
kubectl create secret generic execrelay-secrets \
  --from-literal=database-url="postgresql://execrelay:$DB_PASSWORD@$DB_ENDPOINT:5432/execrelay" \
  --from-literal=nats-url="nats://execrelay:$MQ_PASSWORD@$MQ_ENDPOINT:4222" \
  --from-literal=jwt-secret="$(openssl rand -hex 32)" \
  --from-literal=licenses="$(cat your-licenses.txt)" \
  --from-literal=pagerduty-key="$PAGERDUTY_KEY" \
  --from-literal=slack-webhook-url="$SLACK_WEBHOOK_URL" \
  -n $NAMESPACE
```

## 5. Run Database Migrations

```bash
# Port-forward to RDS
kubectl run -it --rm psql-client \
  --image=postgres:14 \
  --command -- \
  bash

# Inside the pod
psql -h <RDS_ENDPOINT> -U execrelay -d execrelay

# Run migrations
\i /path/to/001_schema.sql
\i /path/to/002_timescale.sql
```

## 6. Monitoring & Logs

### CloudWatch Logs

RDS PostgreSQL logs:
```bash
aws logs tail /aws/rds/instance/execrelay-db/postgresql --follow
```

MQ Broker logs:
```bash
aws logs tail /aws/mq/execrelay --follow
```

### CloudWatch Metrics

RDS:
- DatabaseConnections
- CPUUtilization
- FreeableMemory
- WriteLatency
- ReadLatency

MQ:
- CurrentConnectionCount
- MemoryUsage
- ActiveMessageQueue

## 7. Compliance Infrastructure

These three controls back the claims in [`docs/compliance.md`](../../docs/compliance.md)
and [`docs/disaster-recovery.md`](../../docs/disaster-recovery.md). They are
external cloud configuration (no in-repo Terraform yet — that's Phase 6, see
[`infra/terraform/README.md`](../terraform/README.md)), so they're given here
as runnable CLI recipes. Run each once per environment.

### 7a. WORM audit archive (S3 Object Lock)

A Write-Once-Read-Many bucket for the immutable audit archive (exports of
`admin_audit_log` / `system_events`). Object Lock **must** be enabled at bucket
creation — it cannot be turned on later.

```bash
REGION=us-east-1
AUDIT_BUCKET=execrelay-audit-archive   # must be globally unique

# Object Lock requires versioning; --object-lock-enabled-for-bucket sets both.
aws s3api create-bucket \
  --bucket "$AUDIT_BUCKET" \
  --region "$REGION" \
  --object-lock-enabled-for-bucket \
  $( [ "$REGION" != us-east-1 ] && echo --create-bucket-configuration LocationConstraint="$REGION" )

# Default retention: COMPLIANCE mode means not even the root account can delete
# or shorten the lock before it expires. 7 years (2557 days) for financial audit.
aws s3api put-object-lock-configuration \
  --bucket "$AUDIT_BUCKET" \
  --object-lock-configuration \
  'ObjectLockEnabled=Enabled,Rule={DefaultRetention={Mode=COMPLIANCE,Days=2557}}'

# Encrypt at rest + block all public access.
aws s3api put-bucket-encryption --bucket "$AUDIT_BUCKET" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"aws:kms"}}]}'
aws s3api put-public-access-block --bucket "$AUDIT_BUCKET" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

Minimal IAM policy for the writer principal (put-only; no delete, no lock
bypass — COMPLIANCE mode enforces this regardless, but least-privilege still
applies):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:PutObject"],
    "Resource": "arn:aws:s3:::execrelay-audit-archive/*"
  }]
}
```

### 7b. Cold-archive lifecycle (Glacier transition)

Transition the **backup** bucket (`BACKUP_S3_BUCKET`, the off-host Postgres
dumps from `scripts/backup.sh`) to Glacier Flexible Retrieval after 90 days to
cut storage cost. Do **not** apply this to the Object Lock audit bucket if you
need fast retrieval of recent audit records.

```bash
BACKUP_BUCKET=execrelay-backups   # same value as BACKUP_S3_BUCKET

aws s3api put-bucket-lifecycle-configuration \
  --bucket "$BACKUP_BUCKET" \
  --lifecycle-configuration '{
    "Rules": [{
      "ID": "cold-archive-after-90d",
      "Status": "Enabled",
      "Filter": {"Prefix": ""},
      "Transitions": [{"Days": 90, "StorageClass": "GLACIER"}],
      "NoncurrentVersionTransitions": [{"NoncurrentDays": 90, "StorageClass": "GLACIER"}]
    }]
  }'
```

### 7c. Edge geofence (Cloudflare WAF country block)

Application-layer registration screening (`BLOCKED_REGISTRATION_COUNTRIES` in
`apps/portal-api/app.py`) only sees the registrant's *declared* country and
cannot express region-level sanctions (Crimea/Donetsk/Luhansk have no ISO
country code). A Cloudflare WAF custom rule blocks at the edge by geo-IP,
catching omitted/misstated countries and the sub-country regions.

```bash
# Requires CF_API_TOKEN (Zone WAF edit) and CF_ZONE_ID.
curl -sS -X POST \
  "https://api.cloudflare.com/client/v4/zones/${CF_ZONE_ID}/firewall/rules" \
  -H "Authorization: Bearer ${CF_API_TOKEN}" \
  -H "Content-Type: application/json" \
  --data '[{
    "action": "block",
    "description": "OFAC sanctioned jurisdictions",
    "filter": {
      "expression": "(ip.geoip.country in {\"CU\" \"IR\" \"KP\" \"SY\"}) or (ip.geoip.subdivision_1_iso_code in {\"UA-43\" \"UA-14\" \"UA-09\"})"
    }
  }]'
```

`UA-43` is Crimea; `UA-14` Donetsk; `UA-09` Luhansk. Review the country list
against the current OFAC SDN/sanctions program before enabling, and keep it in
sync with `BLOCKED_REGISTRATION_COUNTRIES`.

## Cost Optimization

### Recommended for Production

| Service | Instance | Monthly Cost | Notes |
|---------|----------|--------------|-------|
| RDS PostgreSQL | db.t3.small | $30 | Multi-AZ: +$30 |
| AWS MQ | mq.t3.micro | $20 | Single-instance |
| EKS Cluster | Variable | $74 | Per cluster |
| EC2 Nodes | t3.medium x 3 | $60 | Varies by usage |
| Data Transfer | Variable | $10-20 | Typical ingress |

**Total: ~$200-250/month**

### Cost Reduction Tips

1. Use Single-AZ RDS for development (saves 50%)
2. Use `db.t3.micro` RDS for testing
3. Use mq.t3.micro for MQ
4. Enable RDS reserved instances for 1-year commitment (25% discount)
5. Use EC2 Spot instances for non-critical workloads (50-70% discount)

## Disaster Recovery

### RDS Backup Strategy

```bash
# Manual snapshot
aws rds create-db-snapshot \
  --db-instance-identifier execrelay-db \
  --db-snapshot-identifier execrelay-db-snapshot-$(date +%Y%m%d)

# Auto-backup: 7 days (enabled by default)
# Point-in-time recovery: Yes (enabled)
```

### Restore from Snapshot

```bash
aws rds restore-db-instance-from-db-snapshot \
  --db-instance-identifier execrelay-db-restore \
  --db-snapshot-identifier execrelay-db-snapshot-20240101 \
  --db-instance-class db.t3.small
```

### NATS Persistence

MQ for NATS uses EBS volumes with automatic snapshots. Enable:
- EBS snapshot lifecycle: daily, retain 7 days
- CloudFormation drift detection: monthly

## Monitoring Checklist

- [ ] RDS automated backups enabled
- [ ] RDS multi-AZ enabled
- [ ] RDS encryption at rest enabled
- [ ] RDS enhanced monitoring enabled
- [ ] MQ CloudWatch logs enabled
- [ ] VPC Flow Logs enabled for network debugging
- [ ] CloudTrail enabled for audit logs
- [ ] AWS Config enabled for compliance
- [ ] Cost Explorer alerts configured

## References

- [AWS RDS PostgreSQL Documentation](https://docs.aws.amazon.com/rds/latest/userguide/CHAP_PostgreSQL.html)
- [AWS MQ for NATS Documentation](https://docs.aws.amazon.com/amazon-mq/latest/developer-guide/nats.html)
- [AWS EKS Best Practices](https://docs.aws.amazon.com/eks/latest/userguide/best-practices.html)

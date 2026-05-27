#!/bin/bash

# Setup GitHub Actions OIDC provider for AWS
# This allows GitHub Actions to authenticate to AWS without static credentials

set -e

ACCOUNT_ID=${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}
GITHUB_ORG=${GITHUB_ORG:-ninadk}
GITHUB_REPO=${GITHUB_REPO:-execrelay}
REGION=${AWS_REGION:-us-east-1}

echo "Setting up GitHub Actions OIDC for AWS..."
echo "Account ID: $ACCOUNT_ID"
echo "GitHub: $GITHUB_ORG/$GITHUB_REPO"
echo "Region: $REGION"

# 1. Create OIDC Identity Provider (if not exists)
echo "Creating GitHub OIDC provider..."
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1 \
  --client-id-list sts.amazonaws.com \
  --region $REGION \
  2>/dev/null || echo "OIDC provider already exists"

# 2. Create IAM role for GitHub Actions
ROLE_NAME="github-actions-role"
TRUST_POLICY='{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::'"$ACCOUNT_ID"':oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": "repo:'"$GITHUB_ORG/$GITHUB_REPO"':*"
        }
      }
    }
  ]
}'

echo "Creating IAM role: $ROLE_NAME"
aws iam create-role \
  --role-name $ROLE_NAME \
  --assume-role-policy-document "$TRUST_POLICY" \
  2>/dev/null || echo "Role already exists"

# 3. Create policy for ECR access
POLICY_NAME="github-actions-ecr-policy"
ECR_POLICY='{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ecr:CreateRepository",
        "ecr:DescribeRepositories",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage",
        "ecr:PutImage",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload",
        "ecr:GetAuthorizationToken"
      ],
      "Resource": "arn:aws:ecr:'"$REGION:$ACCOUNT_ID"':repository/execrelay-*"
    },
    {
      "Effect": "Allow",
      "Action": "ecr:GetAuthorizationToken",
      "Resource": "*"
    }
  ]
}'

echo "Creating IAM policy: $POLICY_NAME"
aws iam put-role-policy \
  --role-name $ROLE_NAME \
  --policy-name $POLICY_NAME \
  --policy-document "$ECR_POLICY"

# 4. Create ECR repositories
echo "Creating ECR repositories..."
for service in ingress bridge dxtrade persist portal-api tasks analytics reports portal-web; do
  aws ecr create-repository \
    --repository-name execrelay-$service \
    --region $REGION \
    2>/dev/null || echo "Repository execrelay-$service already exists"
done

# 5. Setup GitHub repository secrets
echo "GitHub Secrets to set in repository settings:"
echo "  AWS_ACCOUNT_ID = $ACCOUNT_ID"
echo "  AWS_REGION = $REGION"

echo ""
echo "✓ GitHub Actions OIDC setup complete!"
echo ""
echo "Next steps:"
echo "1. Go to your GitHub repository settings"
echo "2. Navigate to Settings → Secrets and variables → Actions"
echo "3. Add the following secrets:"
echo "   - AWS_ACCOUNT_ID: $ACCOUNT_ID"
echo "   - AWS_REGION: $REGION"
echo ""
echo "4. GitHub Actions workflow will now authenticate to AWS automatically"

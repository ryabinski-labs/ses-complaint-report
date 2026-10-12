#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski
#
# Deploy the self-owned SES feedback ledger.
#
# Usage:
#   ./infra/deploy_ledger.sh [stack-name] [region] [artifact-bucket] [topics]
#
#   [stack-name]       CloudFormation stack name (default: ses-complaint-report-ledger).
#   [region]           AWS region (default: us-east-1).
#   [artifact-bucket]  S3 bucket for the packaged Lambda zip (required; or set
#                      LEDGER_ARTIFACT_BUCKET).
#   [topics]           Comma-separated feedback SNS topic NAMES to subscribe the
#                      ingest Lambda to. No default: name your own feedback topics
#                      (e.g. my-app-ses-feedback,my-app-ses-bounces). Leave blank
#                      to deploy the table + Lambda only and wire subscriptions
#                      later.
#
# What it does:
#   1. Packages infra/ledger_ingest/ and uploads it to the artifact bucket.
#   2. Deploys infra/ledger-stack.yaml (DynamoDB table + ingest Lambda + IAM +
#      account-scoped SNS invoke permission).
#   3. Subscribes the ingest Lambda to every named topic. Idempotent: AWS returns
#      the existing subscription ARN when the topic/endpoint pair already exists.
#   4. Prints the table name to set as SESREPORT_LEDGER_TABLE for the report.
#
# Subscriptions live outside the stack on purpose: the topics are owned by other
# project stacks and their names/ARNs vary per deployment. Re-run this after
# adding a feedback topic so the ledger keeps covering every send path (the
# report's "suppression coverage" section is how you spot a gap).

set -euo pipefail

STACK_NAME="${1:-ses-complaint-report-ledger}"
REGION="${2:-us-east-1}"
ARTIFACT_BUCKET="${3:-${LEDGER_ARTIFACT_BUCKET:-}}"
TOPICS="${4:-}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$REPO_ROOT/infra/ledger-stack.yaml"
CODE_DIR="$REPO_ROOT/infra/ledger_ingest"

if [[ -z "$ARTIFACT_BUCKET" ]]; then
    echo "!! No artifact bucket. Pass one as arg 3 or set LEDGER_ARTIFACT_BUCKET." >&2
    exit 2
fi

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
CODE_KEY="ses-complaint-report/ledger_ingest-$(date +%Y%m%d%H%M%S).zip"

echo "==> Packaging $CODE_DIR"
if [[ ! -f "$CODE_DIR/index.py" ]]; then
    echo "!! Missing $CODE_DIR/index.py" >&2
    exit 1
fi
TMP_DIR="$(mktemp -d -t ledger_ingest.XXXXXX)"
TMP_ZIP="$TMP_DIR/ledger_ingest.zip"
# -j flattens the directory so the handler is index.handler at the zip root.
zip -j -q "$TMP_ZIP" "$CODE_DIR/index.py"

echo "==> Uploading s3://$ARTIFACT_BUCKET/$CODE_KEY"
aws s3 cp "$TMP_ZIP" "s3://$ARTIFACT_BUCKET/$CODE_KEY" --region "$REGION" --only-show-errors
rm -rf "$TMP_DIR"

echo "==> Deploying stack $STACK_NAME (region $REGION)"
aws cloudformation deploy \
    --region "$REGION" \
    --stack-name "$STACK_NAME" \
    --template-file "$TEMPLATE" \
    --parameter-overrides \
        LambdaCodeBucket="$ARTIFACT_BUCKET" \
        LambdaCodeKey="$CODE_KEY" \
    --capabilities CAPABILITY_IAM \
    --tags Project=ses-complaint-report Environment=prod ManagedBy=CloudFormation

FUNC_ARN="$(aws cloudformation describe-stacks \
    --region "$REGION" \
    --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='FunctionArn'].OutputValue" \
    --output text)"
TABLE_NAME="$(aws cloudformation describe-stacks \
    --region "$REGION" \
    --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='TableName'].OutputValue" \
    --output text)"

if [[ -z "$FUNC_ARN" || "$FUNC_ARN" == "None" ]]; then
    echo "!! Could not read FunctionArn from stack outputs" >&2
    exit 1
fi
echo "==> Ingest Lambda: $FUNC_ARN"

# --- Subscribe the Lambda to each feedback topic ---------------------------
if [[ -z "$TOPICS" ]]; then
    echo "==> No topics given; skipping subscriptions. Re-run with arg 4 (comma-"
    echo "    separated feedback topic names) once your topics exist."
else
    IFS=',' read -r -a TOPIC_LIST <<<"$TOPICS"
    for TOPIC_NAME in "${TOPIC_LIST[@]}"; do
        TOPIC_NAME="$(echo "$TOPIC_NAME" | xargs)"
        [[ -z "$TOPIC_NAME" ]] && continue
        TOPIC_ARN="arn:aws:sns:${REGION}:${ACCOUNT_ID}:${TOPIC_NAME}"
        echo "==> Subscribing ingest -> $TOPIC_NAME"
        aws sns subscribe \
            --region "$REGION" \
            --topic-arn "$TOPIC_ARN" \
            --protocol lambda \
            --notification-endpoint "$FUNC_ARN" \
            --query SubscriptionArn --output text
    done
fi

echo
echo "==> Done."
echo "    Set this in the report's .env:"
echo "      SESREPORT_LEDGER_TABLE=$TABLE_NAME"
echo
echo "    The report stays read-only; this Lambda is the only writer."
echo "    A 'PendingConfirmation' subscription ARN means SNS has not yet"
echo "    confirmed — the Lambda auto-confirms on first delivery, so trigger a"
echo "    test send (e.g. to complaint@simulator.amazonses.com) to settle it."

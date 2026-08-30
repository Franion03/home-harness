#!/usr/bin/env bash
# Create (or update) the harness Secret directly in the cluster from your
# shell environment. Fast path for bootstrapping and local iteration.
#
# For the GitOps path, use seal-secrets.sh or the GitHub Actions workflow so
# the encrypted SealedSecret is what lives in git.
#
# Usage:
#     export ANTHROPIC_API_KEY=... OPENAI_API_KEY=... HA_TOKEN=... ...
#     scripts/create-secrets.sh
set -euo pipefail

NAMESPACE="${NAMESPACE:-assistant}"
SECRET="${SECRET:-harness-secrets}"

KEYS=(
  CF_ACCOUNT_ID CF_AI_GATEWAY CF_AI_GATEWAY_TOKEN
  ANTHROPIC_API_KEY OPENAI_API_KEY GOOGLE_AI_API_KEY OPENROUTER_API_KEY
  GROQ_API_KEY CF_WORKERS_AI_TOKEN ELEVENLABS_API_KEY DEEPGRAM_API_KEY
  HA_URL HA_TOKEN
  GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET GOOGLE_REFRESH_TOKEN GOOGLE_CALENDAR_ID
  HARNESS_API_KEY
)

args=()
set_count=0
for k in "${KEYS[@]}"; do
  v="${!k-}"
  args+=(--from-literal="$k=${v}")
  [ -n "$v" ] && set_count=$((set_count + 1))
done

kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null

kubectl create secret generic "$SECRET" \
  --namespace="$NAMESPACE" \
  "${args[@]}" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "✓ Secret '$SECRET' applied to namespace '$NAMESPACE' (${set_count}/${#KEYS[@]} values set)."
echo "  Restart to pick it up:  kubectl -n $NAMESPACE rollout restart deploy/harness"

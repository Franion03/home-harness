#!/usr/bin/env bash
# Seal the harness Secret with the cluster's sealed-secrets public cert and
# write the encrypted result into the repo, ready to commit.
#
# Same hybrid pipeline as arr-stack:
#   env/GitHub Secret -> kubeseal -> git -> ArgoCD -> SealedSecrets -> Secret
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="${NAMESPACE:-assistant}"
CERT="${CERT:-$ROOT/.github/sealed-secrets-cert.pem}"
OUT="${OUT:-$ROOT/deploy/base/sealed-secret.yaml}"

command -v kubeseal >/dev/null || { echo "kubeseal is required" >&2; exit 1; }

if [ ! -f "$CERT" ]; then
  echo "==> Fetching the cluster's sealed-secrets public cert..."
  mkdir -p "$(dirname "$CERT")"
  kubeseal --fetch-cert \
    --controller-name=sealed-secrets-controller \
    --controller-namespace=kube-system > "$CERT"
  echo "    saved to $CERT (public — safe to commit)"
fi

# Build the plaintext Secret from the environment, then pipe it straight into
# kubeseal. It is never written to disk.
KEYS=(
  CF_ACCOUNT_ID CF_AI_GATEWAY CF_AI_GATEWAY_TOKEN
  ANTHROPIC_API_KEY OPENAI_API_KEY GOOGLE_AI_API_KEY OPENROUTER_API_KEY
  GROQ_API_KEY CF_WORKERS_AI_TOKEN ELEVENLABS_API_KEY DEEPGRAM_API_KEY
  HA_URL HA_TOKEN
  GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET GOOGLE_REFRESH_TOKEN GOOGLE_CALENDAR_ID
  HARNESS_API_KEY
)
args=()
for k in "${KEYS[@]}"; do
  args+=(--from-literal="$k=${!k-}")
done

kubectl create secret generic harness-secrets \
  --namespace="$NAMESPACE" \
  "${args[@]}" \
  --dry-run=client -o yaml \
  | kubeseal --cert "$CERT" --format yaml -o "$OUT"

echo "✓ Wrote $OUT"
echo "  Add 'sealed-secret.yaml' to deploy/base/kustomization.yaml resources, then commit."

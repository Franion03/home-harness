#!/usr/bin/env bash
# Pack the harness source into two ConfigMaps so it can run on a stock python
# image with no container registry involved.
#
# Regenerate this whenever you change anything under harness/, then:
#     kubectl apply -k deploy/overlays/live
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$ROOT/harness/app"
OUT="$ROOT/deploy/overlays/live"

command -v kubectl >/dev/null || { echo "kubectl is required" >&2; exit 1; }

# ConfigMap keys cannot contain '/', which is why the Python modules are laid
# out flat rather than as a nested package.
py_args=()
for f in "$APP"/*.py; do
  py_args+=(--from-file="$(basename "$f")=$f")
done
py_args+=(--from-file="requirements.txt=$ROOT/harness/requirements.txt")

kubectl create configmap harness-source \
  --namespace=assistant \
  "${py_args[@]}" \
  --dry-run=client -o yaml > "$OUT/source-configmap.yaml"

static_args=()
for f in "$APP"/static/*; do
  static_args+=(--from-file="$(basename "$f")=$f")
done

kubectl create configmap harness-static \
  --namespace=assistant \
  "${static_args[@]}" \
  --dry-run=client -o yaml > "$OUT/static-configmap.yaml"

echo "wrote $OUT/source-configmap.yaml   ($(grep -c '^  [a-zA-Z]' "$OUT/source-configmap.yaml" || true) keys)"
echo "wrote $OUT/static-configmap.yaml"

# A ConfigMap tops out at 1 MiB; warn well before that becomes a mystery.
for f in "$OUT/source-configmap.yaml" "$OUT/static-configmap.yaml"; do
  size=$(wc -c < "$f")
  if [ "$size" -gt 900000 ]; then
    echo "WARNING: $(basename "$f") is ${size} bytes, close to the 1 MiB ConfigMap limit." >&2
    echo "         Build a real image and switch to deploy/overlays/ghcr." >&2
  fi
done

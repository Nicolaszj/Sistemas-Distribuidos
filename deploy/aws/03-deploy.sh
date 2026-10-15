#!/usr/bin/env bash
# Despliega (o actualiza) DFSha en los nodos. Uso:
#   bash deploy/aws/03-deploy.sh <rama> [nodo ...]
#   bash deploy/aws/03-deploy.sh main                 # los 5 nodos
#   bash deploy/aws/03-deploy.sh main dn2             # solo dn2
# Cada nodo recibe por S3 (URL firmada de 10 min, se borra al terminar) su paquete:
# certificados, secretos y variables de su rol. Luego, por SSM, clona la rama,
# construye la imagen Docker y arranca el contenedor con TLS.
source "$(dirname "$0")/config.sh"
load_state

BRANCH="${1:?Indica la rama, por ejemplo: bash deploy/aws/03-deploy.sh main}"
shift
TARGETS=("$@")
[ ${#TARGETS[@]} -eq 0 ] && TARGETS=("${NODES[@]}")

[ -f "$OUT/secrets.env" ] && [ -f "$OUT/certs/ca.crt" ] || {
  echo "Faltan secretos o certificados. Ejecuta primero deploy/aws/02-certs.sh" >&2; exit 1; }

BUCKET=$(bucket_name)
if ! aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  aws s3api create-bucket --bucket "$BUCKET" >/dev/null
  aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
fi

render() {  # plantilla nodo -> variables con las IP reales
  local node="$1" line
  sed -e "s|{{NODE}}|$node|g" -e "s|{{SELF_EIP}}|$(public_ip "$node")|g" "$HERE/nodes/$node.env" |
  while IFS= read -r line; do
    for other in "${NODES[@]}"; do line="${line//\{\{EIP_$other\}\}/$(public_ip "$other")}"; done
    echo "$line"
  done | { grep -v -e '^[[:space:]]*#' -e '^[[:space:]]*$' || true; }
}

mkdir -p "$OUT/logs"
for node in "${TARGETS[@]}"; do
  dir="$OUT/bundle/$node"
  rm -rf "$dir"; mkdir -p "$dir/certs"
  cp "$OUT/certs/ca.crt" "$dir/certs/ca.crt"
  cp "$OUT/certs/$node.crt" "$dir/certs/node.crt"
  cp "$OUT/certs/$node.key" "$dir/certs/node.key"
  cp "$OUT/secrets.env" "$dir/secrets.env"
  cp "$HERE/node-run.sh" "$dir/node-run.sh"
  printf 'NODE=%s\nROLE=%s\nBRANCH=%s\nREPO=%s\n' "$node" "$(role_of "$node")" "$BRANCH" "$REPO_URL" >"$dir/deploy.env"
  {
    echo "DFSHA_TLS_CERT=/certs/node.crt"
    echo "DFSHA_TLS_KEY=/certs/node.key"
    echo "DFSHA_TLS_CA=/certs/ca.crt"
    render "$node"
  } >"$dir/node.env"
  tar -czf "$OUT/bundle/$node.tgz" -C "$dir" .
  aws s3 cp --quiet "$OUT/bundle/$node.tgz" "s3://$BUCKET/$node.tgz"
  url=$(aws s3 presign "s3://$BUCKET/$node.tgz" --expires-in 600)
  echo "Desplegando rama $BRANCH en $node ($(role_of "$node"))..."
  run_on "$node" "set -e
rm -rf /opt/dfsha-bundle && mkdir -p /opt/dfsha-bundle && chmod 700 /opt/dfsha-bundle
curl -sSf '$url' -o /opt/dfsha-bundle/b.tgz
tar -xzf /opt/dfsha-bundle/b.tgz -C /opt/dfsha-bundle && rm /opt/dfsha-bundle/b.tgz
bash /opt/dfsha-bundle/node-run.sh
rm -rf /opt/dfsha-bundle" >"$OUT/logs/$node.log" 2>&1 &
done
FAILED=0
for job in $(jobs -p); do wait "$job" || FAILED=1; done

for node in "${TARGETS[@]}"; do cat "$OUT/logs/$node.log"; echo; done
aws s3 rm --quiet --recursive "s3://$BUCKET/"
rm -rf "$OUT/bundle"

echo "Esperando 15 s a que los DataNodes se registren..."
sleep 15
bash "$HERE/status.sh" --no-start
exit $FAILED

#!/usr/bin/env bash
# Genera los secretos del clúster y los certificados TLS de cada nodo.
#   out/secrets.env    secretos compartidos (JWT de usuarios y secreto entre nodos)
#   out/cn-secrets.env llave AES que comparten los dos ControlNodes (alta disponibilidad)
#   out/certs/ca.crt   CA del clúster: es lo único que necesitan los clientes
#   out/certs/<nodo>.* certificado y llave de cada nodo, con su Elastic IP como SAN
# Nada de out/ se sube a git. Si las IP no cambian, no hace falta repetirlo.
source "$(dirname "$0")/config.sh"
load_state

REPO_ROOT="$(cd "$HERE/../.." && pwd)"
python3 -c "import cryptography" 2>/dev/null || pip3 install --quiet cryptography 2>/dev/null || pip3 install --user --quiet cryptography

if [ ! -f "$OUT/secrets.env" ]; then
  python3 - >"$OUT/secrets.env" <<'EOF'
import secrets
print(f"DFSHA_SECRET={secrets.token_urlsafe(48)}")
print(f"DFSHA_CLUSTER_SECRET={secrets.token_urlsafe(48)}")
EOF
  chmod 600 "$OUT/secrets.env"
  echo "Secretos nuevos en $OUT/secrets.env"
else
  echo "Secretos existentes en $OUT/secrets.env (se reutilizan)"
fi

if [ ! -f "$OUT/cn-secrets.env" ]; then
  # Los DataNodes generan cada uno su llave de bloques y no la comparten. Los dos
  # ControlNodes sí deben tener la misma, porque el respaldo reemplaza al primario.
  python3 -c "import base64, os; print('DFSHA_STORAGE_KEY=' + base64.urlsafe_b64encode(os.urandom(32)).decode())" >"$OUT/cn-secrets.env"
  chmod 600 "$OUT/cn-secrets.env"
  echo "Llave compartida de los ControlNodes en $OUT/cn-secrets.env"
fi

for node in "${NODES[@]}"; do
  dns_var="DNS_$node"
  python3 "$REPO_ROOT/scripts/gen_certs.py" "$node" --out "$OUT/certs" \
    --san "$(public_ip "$node")" "$(private_ip "$node")" "${!dns_var}"
done
chmod 600 "$OUT"/certs/*.key
echo "Listo. Siguiente: bash deploy/aws/03-deploy.sh <rama>"

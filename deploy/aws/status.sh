#!/usr/bin/env bash
# Enciende los nodos apagados (el Learner Lab los apaga al cerrar la sesión) y
# revisa que cada uno responda por HTTPS validando su certificado con la CA.
#   bash deploy/aws/status.sh
source "$(dirname "$0")/config.sh"
load_state

if [ "${1:-}" != "--no-start" ]; then
  stopped=()
  for node in "${NODES[@]}"; do
    state=$(aws ec2 describe-instances --instance-ids "$(instance_id "$node")" \
      --query 'Reservations[0].Instances[0].State.Name' --output text)
    [ "$state" = "stopped" ] && stopped+=("$(instance_id "$node")")
  done
  if [ ${#stopped[@]} -gt 0 ]; then
    echo "Encendiendo ${#stopped[@]} nodo(s) apagado(s)..."
    aws ec2 start-instances --instance-ids "${stopped[@]}" >/dev/null
    aws ec2 wait instance-running --instance-ids "${stopped[@]}"
    echo "Esperando 40 s a que Docker arranque los contenedores..."
    sleep 40
  fi
fi

# Token de nodo (JWT HS256 firmado con el secreto del clúster) para preguntar el rol
# de HA. Se arma solo con la librería estándar de Python porque CloudShell borra los
# paquetes instalados con pip al reiniciar la sesión.
node_token() {
  (
    set -a; source "$OUT/secrets.env"; set +a
    python3 - <<'PY'
import base64, hashlib, hmac, json, os, time
b64 = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b"=")
header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
payload = b64(json.dumps({"typ": "node", "sub": "status", "exp": int(time.time()) + 60}).encode())
signature = hmac.new(os.environ["DFSHA_CLUSTER_SECRET"].encode(), header + b"." + payload, hashlib.sha256).digest()
print((header + b"." + payload + b"." + b64(signature)).decode())
PY
  ) 2>/dev/null || true
}
NODE_TOKEN=$(node_token)

ha_role() {  # rol y época de un ControlNode, p. ej. "primary/0"
  [ -n "$NODE_TOKEN" ] || { echo "?"; return; }
  curl -s --max-time 6 --cacert "$OUT/certs/ca.crt" -H "X-Node-Token: $NODE_TOKEN" \
    "https://$(public_ip "$1"):$PORT/ha/status" |
    python3 -c "import json,sys; d=json.load(sys.stdin); print(f\"{d['role']}/{d['epoch']}\")" 2>/dev/null || echo "-"
}

printf '%-5s %-12s %-16s %-10s %-8s %-11s %s\n' NODO ROL ELASTIC_IP EC2 HTTPS HA URL
for node in "${NODES[@]}"; do
  state=$(aws ec2 describe-instances --instance-ids "$(instance_id "$node")" \
    --query 'Reservations[0].Instances[0].State.Name' --output text)
  if [ "$(role_of "$node")" = datanode ]; then path=/health; else path=/docs; fi
  url="https://$(public_ip "$node"):$PORT$path"
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 --cacert "$OUT/certs/ca.crt" "$url" || true)
  [ "$code" = "200" ] && result="OK" || result="FALLA($code)"
  if [ "$(role_of "$node")" = controlnode ]; then ha=$(ha_role "$node"); else ha=""; fi
  printf '%-5s %-12s %-16s %-10s %-8s %-11s %s\n' "$node" "$(role_of "$node")" "$(public_ip "$node")" "$state" "$result" "$ha" "$url"
done

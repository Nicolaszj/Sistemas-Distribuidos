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

# Token de nodo (firmado con el secreto del clúster) para preguntar el rol de HA.
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
NODE_TOKEN=$(cd "$REPO_ROOT" && set -a && source "$OUT/secrets.env" && set +a && python3 -c \
  "import os; from dfsha.cluster_auth import issue_node_token; print(issue_node_token(os.environ['DFSHA_CLUSTER_SECRET'], 'status'))" 2>/dev/null || true)

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

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

printf '%-5s %-12s %-16s %-10s %-8s %s\n' NODO ROL ELASTIC_IP EC2 HTTPS URL
for node in "${NODES[@]}"; do
  state=$(aws ec2 describe-instances --instance-ids "$(instance_id "$node")" \
    --query 'Reservations[0].Instances[0].State.Name' --output text)
  if [ "$(role_of "$node")" = datanode ]; then path=/health; else path=/docs; fi
  url="https://$(public_ip "$node"):$PORT$path"
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 --cacert "$OUT/certs/ca.crt" "$url" || true)
  [ "$code" = "200" ] && result="OK" || result="FALLA($code)"
  printf '%-5s %-12s %-16s %-10s %-8s %s\n' "$node" "$(role_of "$node")" "$(public_ip "$node")" "$state" "$result" "$url"
done

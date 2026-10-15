#!/usr/bin/env bash
# Crea la infraestructura del clúster DFSha en AWS:
#   - Security Group dfsha-sg: solo el puerto 8000 (HTTPS de la API). Sin SSH.
#   - 5 EC2 Amazon Linux 2023 (cn1, cn2, dn1, dn2, dn3) repartidas en 3 zonas,
#     con Docker y administradas por SSM (LabInstanceProfile), IMDSv2 obligatorio.
#   - Una Elastic IP por nodo, para que las direcciones (y los certificados) no
#     cambien cuando el Learner Lab apaga y vuelve a encender las máquinas.
# Se puede ejecutar varias veces: reutiliza lo que ya exista.
source "$(dirname "$0")/config.sh"

echo "== Red =="
VPC=$(aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)
[ "$VPC" = "None" ] && { echo "No hay VPC por defecto en $AWS_DEFAULT_REGION" >&2; exit 1; }
echo "VPC por defecto: $VPC"

SG=$(aws ec2 describe-security-groups --filters Name=vpc-id,Values="$VPC" Name=group-name,Values="$PROJECT-sg" \
  --query 'SecurityGroups[0].GroupId' --output text)
if [ "$SG" = "None" ]; then
  SG=$(aws ec2 create-security-group --group-name "$PROJECT-sg" \
    --description "DFSha: API HTTPS 8000 de ControlNodes y DataNodes" --vpc-id "$VPC" \
    --tag-specifications "$(tag_spec security-group sg)" --query GroupId --output text)
  aws ec2 authorize-security-group-ingress --group-id "$SG" \
    --ip-permissions "IpProtocol=tcp,FromPort=$PORT,ToPort=$PORT,IpRanges=[{CidrIp=0.0.0.0/0,Description=DFSha-API-HTTPS}]" >/dev/null
  echo "Security Group creado: $SG"
else
  echo "Security Group existente: $SG"
fi

USER_DATA=$(cat <<'EOF'
#!/bin/bash
dnf install -y docker git
systemctl enable --now docker
EOF
)

echo "== Instancias =="
for node in "${NODES[@]}"; do
  id=$(aws ec2 describe-instances \
    --filters Name=tag:project,Values="$PROJECT" Name=tag:node,Values="$node" \
              Name=instance-state-name,Values=pending,running,stopping,stopped \
    --query 'Reservations[0].Instances[0].InstanceId' --output text)
  if [ "$id" = "None" ]; then
    subnet=$(aws ec2 describe-subnets --filters Name=vpc-id,Values="$VPC" \
      Name=availability-zone,Values="${ZONE[$node]}" Name=default-for-az,Values=true \
      --query 'Subnets[0].SubnetId' --output text)
    id=$(aws ec2 run-instances --image-id "$AMI" --instance-type "$INSTANCE_TYPE" \
      --subnet-id "$subnet" --security-group-ids "$SG" \
      --iam-instance-profile Name="$INSTANCE_PROFILE" \
      --metadata-options HttpTokens=required,HttpEndpoint=enabled \
      --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=16,VolumeType=gp3}' \
      --user-data "$USER_DATA" \
      --tag-specifications "$(tag_spec instance "$node")" "$(tag_spec volume "$node")" \
      --query 'Instances[0].InstanceId' --output text)
    echo "$node: creada $id en ${ZONE[$node]}"
  else
    echo "$node: existente $id"
    aws ec2 start-instances --instance-ids "$id" >/dev/null 2>&1 || true
  fi
  printf -v "ID_$node" '%s' "$id"
done

ALL_IDS=()
for node in "${NODES[@]}"; do ALL_IDS+=("$(instance_id "$node")"); done
echo "Esperando a que las 5 instancias estén en running..."
aws ec2 wait instance-running --instance-ids "${ALL_IDS[@]}"

echo "== Elastic IPs =="
for node in "${NODES[@]}"; do
  alloc=$(aws ec2 describe-addresses --filters Name=tag:project,Values="$PROJECT" Name=tag:node,Values="$node" \
    --query 'Addresses[0].AllocationId' --output text)
  if [ "$alloc" = "None" ]; then
    alloc=$(aws ec2 allocate-address --domain vpc \
      --tag-specifications "$(tag_spec elastic-ip "$node")" --query AllocationId --output text)
  fi
  aws ec2 associate-address --instance-id "$(instance_id "$node")" --allocation-id "$alloc" >/dev/null
done

echo "== Guardando direcciones en $STATE =="
{
  echo "# Generado por 01-infra.sh el $(date -u +%FT%TZ)"
  echo "SG=$SG"
  for node in "${NODES[@]}"; do
    read -r pub priv dns <<<"$(aws ec2 describe-instances --instance-ids "$(instance_id "$node")" \
      --query 'Reservations[0].Instances[0].[PublicIpAddress,PrivateIpAddress,PublicDnsName]' --output text)"
    echo "ID_$node=$(instance_id "$node")"
    echo "EIP_$node=$pub"
    echo "PRIV_$node=$priv"
    echo "DNS_$node=$dns"
  done
} >"$STATE"
load_state

echo "== Esperando a que SSM vea los 5 nodos (hasta 6 min) =="
for _ in $(seq 1 36); do
  online=$(aws ssm describe-instance-information \
    --filters "Key=InstanceIds,Values=$(IFS=,; echo "${ALL_IDS[*]}")" \
    --query 'length(InstanceInformationList[?PingStatus==`Online`])' --output text)
  echo "  nodos en línea: $online/5"
  [ "$online" = "5" ] && break
  sleep 10
done

echo
printf '%-5s %-12s %-20s %-16s %-15s\n' NODO ROL INSTANCIA ELASTIC_IP ZONA
for node in "${NODES[@]}"; do
  printf '%-5s %-12s %-20s %-16s %-15s\n' "$node" "$(role_of "$node")" "$(instance_id "$node")" "$(public_ip "$node")" "${ZONE[$node]}"
done
[ "$online" = "5" ] || { echo "AVISO: no todos los nodos están en SSM todavía; espera 2 min y vuelve a correr este script." >&2; exit 1; }
echo "Listo. Siguiente: bash deploy/aws/02-certs.sh"

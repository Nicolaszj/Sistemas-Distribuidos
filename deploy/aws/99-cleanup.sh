#!/usr/bin/env bash
# Borra TODO lo creado para DFSha: instancias, Elastic IPs, Security Group y bucket.
#   bash deploy/aws/99-cleanup.sh --confirmar
source "$(dirname "$0")/config.sh"
[ "${1:-}" = "--confirmar" ] || { echo "Esto borra el clúster. Ejecuta: bash deploy/aws/99-cleanup.sh --confirmar"; exit 1; }

ids=$(aws ec2 describe-instances --filters Name=tag:project,Values="$PROJECT" \
  Name=instance-state-name,Values=pending,running,stopping,stopped \
  --query 'Reservations[].Instances[].InstanceId' --output text)
if [ -n "$ids" ]; then
  aws ec2 terminate-instances --instance-ids $ids >/dev/null
  echo "Terminando instancias: $ids"
  aws ec2 wait instance-terminated --instance-ids $ids
fi
for alloc in $(aws ec2 describe-addresses --filters Name=tag:project,Values="$PROJECT" --query 'Addresses[].AllocationId' --output text); do
  aws ec2 release-address --allocation-id "$alloc" && echo "Elastic IP liberada: $alloc"
done
sg=$(aws ec2 describe-security-groups --filters Name=group-name,Values="$PROJECT-sg" --query 'SecurityGroups[0].GroupId' --output text)
[ "$sg" != "None" ] && aws ec2 delete-security-group --group-id "$sg" && echo "Security Group borrado: $sg"
BUCKET=$(bucket_name)
if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  aws s3 rb "s3://$BUCKET" --force >/dev/null && echo "Bucket borrado: $BUCKET"
fi
rm -rf "$OUT"
echo "Limpieza completa."

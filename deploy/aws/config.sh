# Configuración común del despliegue de DFSha en AWS (AWS Academy Learner Lab).
# Lo cargan los demás scripts con: source "$(dirname "$0")/config.sh"
# Se ejecutan desde AWS CloudShell, en la carpeta del repositorio.

set -euo pipefail

export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"

PROJECT="dfsha"
REPO_URL="${DFSHA_REPO_URL:-https://github.com/Nicolaszj/Sistemas-Distribuidos.git}"
INSTANCE_TYPE="${DFSHA_INSTANCE_TYPE:-t3.small}"
INSTANCE_PROFILE="LabInstanceProfile"
AMI="resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
PORT=8000

# Nodo -> zona de disponibilidad. Los nodos quedan en zonas distintas para que la
# caída de una zona no tumbe todo el clúster.
NODES=(cn1 cn2 dn1 dn2 dn3)
declare -A ZONE=(
  [cn1]=us-east-1a [cn2]=us-east-1b
  [dn1]=us-east-1a [dn2]=us-east-1b [dn3]=us-east-1c
)

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$HERE/out"
STATE="$OUT/cluster.env"
mkdir -p "$OUT"
chmod 700 "$OUT"

role_of() { case "$1" in cn*) echo controlnode ;; dn*) echo datanode ;; esac; }

tag_spec() {
  echo "ResourceType=$1,Tags=[{Key=Name,Value=$PROJECT-$2},{Key=project,Value=$PROJECT},{Key=node,Value=$2}]"
}

load_state() {
  if [ ! -f "$STATE" ]; then
    echo "No existe $STATE. Ejecuta primero deploy/aws/01-infra.sh" >&2
    exit 1
  fi
  # shellcheck disable=SC1090
  source "$STATE"
}

instance_id() { local v="ID_$1"; echo "${!v}"; }
public_ip() { local v="EIP_$1"; echo "${!v}"; }
private_ip() { local v="PRIV_$1"; echo "${!v}"; }

bucket_name() {
  local account
  account=$(aws sts get-caller-identity --query Account --output text)
  echo "$PROJECT-deploy-$account"
}

# Ejecuta un script bash como root en un nodo con SSM Run Command (sin SSH ni
# puerto 22) y muestra su salida. Uso: run_on dn1 'docker ps'
run_on() {
  local node="$1" script="$2" id command_id status
  id=$(instance_id "$node")
  command_id=$(aws ssm send-command \
    --instance-ids "$id" \
    --document-name AWS-RunShellScript \
    --comment "$PROJECT $node" \
    --timeout-seconds 1200 \
    --parameters "$(python3 -c 'import json,sys; print(json.dumps({"commands": [sys.stdin.read()]}))' <<<"$script")" \
    --query Command.CommandId --output text)

  while true; do
    status=$(aws ssm get-command-invocation --command-id "$command_id" --instance-id "$id" \
      --query Status --output text 2>/dev/null || echo Pending)
    case "$status" in
      Pending|InProgress|Delayed) sleep 5 ;;
      *) break ;;
    esac
  done

  aws ssm get-command-invocation --command-id "$command_id" --instance-id "$id" \
    --query StandardOutputContent --output text | sed "s/^/[$node] /"
  if [ "$status" != "Success" ]; then
    aws ssm get-command-invocation --command-id "$command_id" --instance-id "$id" \
      --query StandardErrorContent --output text | sed "s/^/[$node][error] /"
    echo "[$node] Estado: $status" >&2
    return 1
  fi
}

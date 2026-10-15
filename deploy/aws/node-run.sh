#!/bin/bash
# Se ejecuta como root DENTRO de cada EC2 (lo lanza 03-deploy.sh por SSM).
# Descarga la rama pedida, construye la imagen y (re)crea el contenedor "dfsha".
set -euo pipefail
cd "$(dirname "$0")"
source ./deploy.env   # NODE, ROLE, BRANCH, REPO

command -v git >/dev/null || dnf install -y -q git
command -v docker >/dev/null || dnf install -y -q docker
systemctl enable --now docker >/dev/null 2>&1

install -d -m 700 /opt/dfsha /opt/dfsha/certs
install -m 644 certs/ca.crt certs/node.crt /opt/dfsha/certs/
install -m 600 certs/node.key /opt/dfsha/certs/
cat secrets.env node.env >/opt/dfsha/node.env
chmod 600 /opt/dfsha/node.env

if [ ! -d /opt/dfsha/src/.git ]; then git clone -q "$REPO" /opt/dfsha/src; fi
git -C /opt/dfsha/src fetch -q --prune origin
git -C /opt/dfsha/src checkout -q -B "$BRANCH" "origin/$BRANCH"
echo "Código: rama $BRANCH, commit $(git -C /opt/dfsha/src log --oneline -1)"

if [ "$ROLE" = datanode ] && [ ! -f /opt/dfsha/src/dfsha/server/datanode.py ]; then
  echo "ERROR: la rama $BRANCH todavía no tiene DataNode (dfsha/server/datanode.py)."
  exit 1
fi

docker build -q -t dfsha:latest /opt/dfsha/src >/dev/null
docker rm -f dfsha >/dev/null 2>&1 || true
docker run -d --name dfsha --restart unless-stopped -p 8000:8000 \
  -v dfsha-data:/data -v /opt/dfsha/certs:/certs:ro \
  --env-file /opt/dfsha/node.env dfsha:latest >/dev/null
sleep 6
docker ps -a --filter name=dfsha --format 'Contenedor {{.Names}}: {{.Status}}'
docker logs --tail 4 dfsha 2>&1

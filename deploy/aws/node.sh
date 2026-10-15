#!/usr/bin/env bash
# Opera el contenedor de un nodo sin SSH (por SSM). Sirve para las pruebas de fallos.
#   bash deploy/aws/node.sh dn2 stop      # simula la caída del DataNode dn2
#   bash deploy/aws/node.sh dn2 start     # lo vuelve a levantar
#   bash deploy/aws/node.sh cn1 logs      # últimas 40 líneas del log
#   bash deploy/aws/node.sh dn1 ps        # estado del contenedor y bloques guardados
#   bash deploy/aws/node.sh dn1 peek      # primeros bytes de un bloque en disco (cifrado)
source "$(dirname "$0")/config.sh"
load_state

node="${1:?Indica el nodo: cn1 cn2 dn1 dn2 dn3}"
action="${2:?Indica la acción: stop start restart logs ps peek}"
case "$action" in
  stop|start|restart) cmd="docker $action dfsha && docker ps -a --filter name=dfsha --format '{{.Names}}: {{.Status}}'" ;;
  logs) cmd="docker logs --tail 40 dfsha 2>&1" ;;
  ps) cmd="docker ps -a --filter name=dfsha --format '{{.Names}}: {{.Status}}'; echo \"Bloques guardados: \$(docker exec dfsha sh -c 'find /data/blocks -type f 2>/dev/null | wc -l')\"" ;;
  peek) cmd="docker exec dfsha sh -c 'f=\$(find /data/blocks -type f | head -1); [ -n \"\$f\" ] || { echo \"Este nodo no tiene bloques todavía\"; exit 0; }; echo \"Bloque: \$f\"; head -c 48 \"\$f\" | od -A d -c'" ;;
  *) echo "Acción no válida: $action" >&2; exit 1 ;;
esac
run_on "$node" "$cmd"

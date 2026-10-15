# Replicación de bloques y consistencia de datos

Complementa la alta disponibilidad del ControlNode (`docs/alta-disponibilidad-controlnode.md`):
allí se replican los **metadatos**; aquí se replican los **datos**, para que la caída de
un DataNode no deje bloques inaccesibles.

## 1. Escritura con factor de replicación

Cada bloque se guarda en `min(DFSHA_REPLICATION_FACTOR, DataNodes vivos)` nodos (3 por
defecto). El ControlNode envía el bloque a sus réplicas y solo cuando **todas** confirman
lo registra en `block_locations`. Si no se alcanzan las réplicas necesarias, borra las que
sí se escribieron y la subida falla con 503: nunca queda un archivo registrado con menos
réplicas de las que se prometieron en el momento de escribir.

El orden en que se prueban los nodos rota con el número de bloque, así que la réplica
"principal" (la primera que se lee) de bloques consecutivos cae en nodos distintos y la
carga de lectura queda repartida. Si un nodo falla durante la escritura se prueba el
siguiente.

## 2. Lectura con failover entre réplicas

El plan de lectura (`GET /files/blocks`) entrega por bloque `url` (la réplica preferida,
siempre de un nodo vivo) y `replicas` (todas, primero las de nodos vivos). El cliente
intenta en orden y pasa a la siguiente réplica si:

- el DataNode no responde o devuelve error, o
- el contenido no coincide con el SHA-256 del bloque (réplica dañada).

`GET /files/download` del ControlNode aplica la misma regla en el servidor.

## 3. Re-replicación automática (DataNode ↔ DataNode)

Un hilo del ControlNode **primario** revisa cada `DFSHA_REREPLICATION_SECONDS` (15 s) los
bloques con menos réplicas **vivas** que `min(factor, DataNodes vivos)`. Por cada réplica
faltante elige un DataNode vivo que tenga el bloque y otro que no, y le pide al primero:

```
POST /blocks/{id}/copy-to        (DataNode origen, autenticado con token de nodo)
{"target_url": "https://<DataNode destino>:8000"}
```

El DataNode origen descifra el bloque con su llave, se firma un ticket de escritura con
el secreto del clúster y lo sube al destino (`PUT /blocks/{id}`), que lo vuelve a cifrar
con su propia llave. El ControlNode registra la nueva ubicación. Así, cuando un DataNode
vuelve después de una caída, recibe las réplicas de lo que se escribió mientras no estaba.

## 4. Modelo de consistencia

| Aspecto | Garantía |
| --- | --- |
| Metadatos | Un único primario los modifica (fencing por época, ver HA). |
| Bloques | Inmutables (WORM). Reescribir un archivo crea bloques nuevos y cambia los metadatos en una sola transacción; los bloques viejos se borran después. Dos réplicas de un bloque no pueden divergir: solo pueden faltar. |
| Visibilidad | Un archivo aparece en `ls` solo cuando todas sus réplicas iniciales están escritas. Un lector ve la versión vieja o la nueva, nunca una mezcla. |
| Integridad | SHA-256 por bloque y por archivo, verificado por el cliente en cada lectura. |
| Durabilidad | Con factor 3, un bloque sobrevive a la caída de 2 de sus 3 DataNodes; la re-replicación devuelve el factor cuando hay nodos disponibles. |

## 5. Configuración

| Variable | Default | Descripción |
| --- | --- | --- |
| `DFSHA_REPLICATION_FACTOR` | `3` | Réplicas por bloque (se usan `min(factor, nodos vivos)`) |
| `DFSHA_REREPLICATION_SECONDS` | `15` | Intervalo de revisión de réplicas faltantes (`0` = desactivado) |

## 6. Limitaciones

- La escritura pasa por el ControlNode, que envía el bloque a cada réplica. Un pipeline
  cliente → DN1 → DN2 → DN3 como el de HDFS reduciría el tráfico del ControlNode.
- No se eliminan réplicas sobrantes: con factor 3 y 3 DataNodes no pueden existir, pero
  con más nodos un bloque podría quedar con más copias tras una re-replicación.

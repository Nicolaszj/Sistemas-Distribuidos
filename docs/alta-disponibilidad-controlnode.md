# Alta disponibilidad del ControlNode

Documento de diseño y operación de la tarea 4 del hito 3: un ControlNode de
respaldo (standby) que replica los metadatos del principal y toma el control si
este cae, sin romper el contrato del cliente ni la arquitectura de DataNodes.

## 1. Problema

Hasta el hito 2, DFSha tiene un único ControlNode: es el dueño de los metadatos
(usuarios, directorios, archivos, bloques, ubicación de bloques) y el único que
atiende a clientes y DataNodes. Si ese proceso cae, el sistema entero queda
inoperable aunque los DataNodes sigan vivos. Esta tarea elimina ese punto único
de falla con una pareja **primary / standby**.

## 2. Arquitectura

```
        clientes (lista de URLs)                 DataNodes (lista de URLs)
               |  reintento                            |  registro / heartbeat
               v                                        v
      +------------------+   GET /ha/oplog   +------------------+
      | ControlNode A    |<------------------| ControlNode B    |
      | rol = primary    |   GET /ha/snapshot| rol = standby    |
      | epoch = N        |------------------>| epoch = N (sigue) |
      +------------------+                   +------------------+
         metadata.db (autoritativa)            metadata.db (réplica)
```

- El **primary** atiende todo como hoy y es la única fuente de verdad.
- El **standby** no atiende a clientes: solo replica el oplog del primary y vigila
  su salud. A las peticiones de cliente responde `503 {"error":"not_primary"}`.
- Clientes y DataNodes conocen ambas URLs y reintentan contra la que responda.

El modo HA es **opcional**: sin `DFSHA_HA_PEER_URL` y con `DFSHA_HA_ROLE=primary`
(el valor por defecto), un nodo se comporta exactamente como antes del hito 3.

## 3. Sincronización de metadatos (oplog + snapshot)

### 3.1 Oplog por triggers de SQLite

Cada escritura de metadatos se registra en una tabla `oplog` mediante **triggers
de SQLite** (`AFTER INSERT/UPDATE/DELETE`) instalados por el nodo que actúa como
primary. Ventajas frente a instrumentar cada función: ninguna escritura se escapa
y se toca mínimamente el código compartido.

Cada entrada tiene: `seq` (secuencia monótona), `ts`, `tbl`, `op`, `pk` (clave
primaria en JSON) y `payload` (fila completa en JSON para INSERT/UPDATE; NULL en
DELETE). El standby reaplica con `INSERT OR REPLACE` o `DELETE` por clave, de forma
**idempotente**.

Tablas replicadas: `users`, `directories`, `files`, `blocks`, `api_keys`,
`groups`, `group_members`, `acl`, `locks`, `nodes`, `block_locations`.

**Excepción (ruido de heartbeat):** el trigger `UPDATE` de `nodes` lleva la guarda
`WHEN NEW.url IS NOT OLD.url OR NEW.status IS NOT OLD.status`, así que los
heartbeats (que solo mueven `last_heartbeat`) **no** generan oplog. Esto evita
agotar la retención con miles de entradas inútiles. El estado de liveness se
reconstruye tras el failover con el periodo de gracia (§6) y los re-registros.

### 3.2 Snapshot inicial

Cuando el standby arranca o queda por detrás del punto más antiguo del oplog (por
poda), pide `GET /ha/snapshot`: una copia binaria y consistente de `metadata.db`
hecha con la **API de backup online de SQLite** (segura con escrituras
concurrentes). El standby la escribe tal cual y continúa con el oplog incremental.

### 3.3 Modelo pull

El standby **pide** (`GET /ha/oplog?after=<seq>&ack=<seq>&epoch=<e>`) cada
`DFSHA_HA_HEARTBEAT_SECONDS`. El mismo request sirve de heartbeat entre
ControlNodes: no hace falta una conexión aparte. `ack` permite al primary podar lo
ya confirmado.

## 4. Protocolo ControlNode ↔ ControlNode

| Método | Ruta | Auth | Para qué |
| --- | --- | --- | --- |
| GET | `/ha/status` | token de nodo | rol, época, `applied_seq`, par, liveness |
| GET | `/ha/oplog?after=&ack=&epoch=` | token de nodo | entregar entradas nuevas; heartbeat |
| GET | `/ha/snapshot` | token de nodo | copia binaria de la base |
| POST | `/ha/promote` | token de nodo + **admin** | promoción manual asistida |

Todos los `/ha/*` exigen un **token de nodo** firmado con `DFSHA_CLUSTER_SECRET`
(nunca login de usuario). `/ha/promote` exige además el claim de operador (`adm`),
emitido por quien posee el secreto del clúster. Todo respeta el TLS del servidor.

## 5. Época y anti split-brain (fencing)

La **época** es un entero monótono en la tabla `ha_state`, durable en cada nodo.

- **Promoción** (automática por timeout o manual por `POST /ha/promote`):
  `epoch += 1`, el nodo pasa a `primary` e instala los triggers del oplog.
- **Regla de fencing:** cuando un ControlNode ve en su par una **época mayor** que
  la propia, se **degrada a standby** y adopta esa época. Un primary viejo que
  vuelve descubre la época mayor en su primer contacto (`/ha/oplog` responde con la
  época del nuevo primary, o el viejo recibe una petición con época mayor) y se
  rinde: deja de generar oplog y pasa a replicar del nuevo primary.
- Como solo hay 2 nodos, la época nueva siempre supera a la del primary viejo, así
  que no quedan dos primaries estables sirviendo escrituras.

## 6. Periodo de gracia tras la promoción

Al promoverse, el `last_heartbeat` replicado de los DataNodes quedó viejo (los
heartbeats no se replican, §3.1). Si el nuevo primary aplicara el timeout normal,
marcaría caídos a DataNodes vivos. Por eso, durante `DFSHA_HA_GRACE_SECONDS` tras
la promoción, el nuevo primary **no excluye** DataNodes por heartbeat viejo y
acepta sus re-registros para refrescar el estado. Pasada la gracia, vuelve la regla
normal de timeout.

## 7. Protocolo cliente ↔ ControlNode

- El cliente acepta **una lista** de URLs (`--server a,b` o `DfsClient([...])`),
  manteniendo compatibilidad con una sola URL.
- Ante un fallo de conexión o un `503 {"error":"not_primary"}`, reintenta contra
  las otras URLs y, al acertar, recuerda ese nodo como el actual.
- Operaciones de lectura (`ls`, `stat`, `blocks`, `get`) y de escritura (`put`,
  etc.) reintentan **solo** ante esos dos casos (conexión o not_primary). Otros
  errores ya evaluados por el servidor (409 ya existe, 423 bloqueado) **no** se
  reintentan.
- `put` rebobina el origen antes de cada intento (o lo materializa si no es
  rebobinable) para re-enviar el archivo completo. La subida en el servidor es
  **atómica**: una subida interrumpida no deja el archivo visible ni bloques
  huérfanos, así que un reintento no acumula basura.
- El contrato de descarga por bloques (`GET /files/blocks` → `GET /blocks/{id}`)
  **no cambia**. Las URLs de bloque apuntan a los DataNodes y van directas, sin
  failover de ControlNode. Los tickets de bloque siguen válidos tras el failover
  porque `DFSHA_CLUSTER_SECRET` es idéntico en ambos ControlNodes.

## 8. Configuración

| Variable | Default | Descripción |
| --- | --- | --- |
| `DFSHA_HA_ROLE` | `primary` | Rol inicial: `primary` o `standby` |
| `DFSHA_HA_PEER_URL` | — | URL del otro ControlNode (vacío = HA desactivado) |
| `DFSHA_HA_PEER_CA` | hereda `DFSHA_NODE_CA` | CA para validar el TLS del par |
| `DFSHA_HA_HEARTBEAT_SECONDS` | `3` | Intervalo de sync/heartbeat del standby |
| `DFSHA_HA_TIMEOUT_SECONDS` | `10` | Sin respuesta del primary ⇒ caído (≥ 3× heartbeat) |
| `DFSHA_HA_GRACE_SECONDS` | `30` | Gracia de DataNodes tras promoción |
| `DFSHA_HA_OPLOG_RETENTION` | `10000` | Máx. entradas de oplog antes de podar |
| `DFSHA_CONTROL_URLS` | — | Lista de ControlNodes para el DataNode (coma) |

**Secretos idénticos (obligatorio en HA):** `DFSHA_CLUSTER_SECRET`, `DFSHA_SECRET`
y `DFSHA_STORAGE_KEY` deben ser los mismos en ambos ControlNodes y pasarse por
entorno. Si se dejan autogenerar, cada nodo tendría secretos distintos y el
failover fallaría (tokens de nodo/usuario y tickets de bloque dejarían de validar).

## 9. Guion de la demo (paso a paso)

Requiere Docker. Levanta primary (A, puerto 8000), standby (B, puerto 8001) y dos
DataNodes.

```bash
docker compose up --build -d
```

1. **Crear usuario y subir un archivo contra el principal (A).**
   ```bash
   python -m dfsha.client.cli --server http://localhost:8000,http://localhost:8001 \
       --user demo --register
   # dentro del shell:
   mkdir /datos
   put archivo.bin /datos/archivo.bin
   ls /datos
   ```
   `ls` debe mostrar `archivo.bin`.

2. **Ver el estado de HA de ambos nodos.**
   ```bash
   # token de nodo firmado con el secreto del clúster (ver scripts del repo)
   curl -s -H "X-Node-Token: <token>" http://localhost:8000/ha/status   # role=primary, epoch=0
   curl -s -H "X-Node-Token: <token>" http://localhost:8001/ha/status   # role=standby, applied_seq>0
   ```
   El standby debe tener `applied_seq > 0` (ya replicó la subida).

3. **Matar el principal.**
   ```bash
   docker compose stop controlnode-a
   ```

4. **Esperar la promoción** (`DFSHA_HA_TIMEOUT_SECONDS = 6` en la demo).
   ```bash
   curl -s -H "X-Node-Token: <token>" http://localhost:8001/ha/status   # role=primary, epoch=1
   ```

5. **Operar contra el nuevo principal (B) sin reconfigurar el cliente.** Como el
   cliente apunta a ambas URLs, reintenta solo contra B:
   ```bash
   # mismo comando de cliente; ls/get/put siguen funcionando
   ls /datos
   get /datos/archivo.bin copia.bin   # debe coincidir con el original
   put otro.bin /datos/otro.bin
   ```

6. **Volver a levantar el viejo principal (A).**
   ```bash
   docker compose start controlnode-a
   curl -s -H "X-Node-Token: <token>" http://localhost:8000/ha/status
   ```
   A descubre la época mayor de B y queda como **standby** (epoch=1). No vuelve a
   actuar como primary (fencing). A sus peticiones de cliente responde 503
   not_primary.

7. **Caída del principal en medio de un `put`.** Inicia una subida grande y detén
   `controlnode-a` a mitad; el cliente reintenta la subida completa contra B. Al
   terminar, `ls`/`stat` del archivo deben mostrarlo íntegro (o no mostrarlo si el
   reintento aún no acabó), **nunca a medias**, y no deben quedar bloques huérfanos
   en los DataNodes.

```bash
docker compose down -v   # limpiar al terminar
```

## 10. Limitaciones conocidas

1. **Replicación asíncrona con ventana de pérdida.** El standby va unos
   `DFSHA_HA_HEARTBEAT_SECONDS` por detrás. Si el primary cae justo después de
   confirmar una escritura al cliente pero antes de que el standby la replique, esa
   última operación se pierde en la promoción. Es un compromiso consciente a favor
   del rendimiento; la ventana se documenta y se puede reducir bajando el intervalo.

2. **Partición de red simétrica sin árbitro (split-brain transitorio).** Con solo
   2 nodos y sin quórum, si la red entre A y B se corta pero ambos siguen visibles
   para parte de los clientes, el standby podría promoverse y existir dos primaries
   hasta que la red se restablezca. Se mitiga con un timeout holgado; la solución
   correcta requiere un tercer árbitro (Raft/ZooKeeper), fuera del alcance de esta
   tarea. Es la misma clase de limitación que un NameNode HA sin journal quorum.

3. **Los bloques todavía no están replicados.** Esta rama replica solo los
   **metadatos** del ControlNode. Cada bloque sigue viviendo en un único DataNode;
   si ese DataNode cae, sus bloques no están disponibles aunque el ControlNode haga
   failover. La replicación de bloques con factor 3, la re-replicación automática y
   la lectura con failover entre réplicas son **RF3** y se implementan en las ramas
   siguientes (`feature/replicacion-rf3`, `feature/re-replicacion`,
   `feature/lectura-failover`), partiendo de la rama de DataNodes ya integrada.

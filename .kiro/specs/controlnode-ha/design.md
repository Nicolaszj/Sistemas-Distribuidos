# Diseño — ControlNode de respaldo (Alta disponibilidad)

Este documento describe cómo se implementa la tarea 4 (ControlNode standby con
failover) sobre el DFSha actual, respetando el contrato del cliente y la
arquitectura de DataNodes del hito 2.

## 1. Visión general

Se despliega una **pareja de ControlNodes**: uno `primary` y uno `standby`,
configurados por `DFSHA_HA_*`. El primary es la única fuente de verdad y atiende
clientes y DataNodes. El standby mantiene una copia de los metadatos replicando
un **oplog** (log de operaciones) del primary y vigila su salud. Si el primary
cae, el standby se **autopromueve** incrementando una **época** (fencing) y pasa
a atender. Clientes y DataNodes conocen las dos URLs y reintentan contra la que
responda como primary.

```
          clientes (lista de URLs)              DataNodes (lista de URLs)
                 |   reintento                       |  registro/heartbeat
                 v                                    v
        +------------------+   oplog pull   +------------------+
        |  ControlNode A   |<---------------|  ControlNode B   |
        |  rol=primary     |   snapshot     |  rol=standby     |
        |  epoch=N         |--------------->|  epoch=N (sigue)  |
        +------------------+                +------------------+
                 ^   metadata.db (autoritativa)        metadata.db (réplica)
```

El modo HA es **opcional**: sin `DFSHA_HA_PEER_URL` y con rol por defecto
`primary`, el nodo se comporta exactamente como hoy (monolítico o distribuido con
un solo ControlNode). No se rompe ningún flujo existente.

## 2. Decisiones de diseño

| # | Decisión | Alternativa descartada | Por qué |
|---|----------|------------------------|---------|
| D1 | Captura de cambios con **triggers de SQLite** que escriben en una tabla `oplog`. | Instrumentar cada función de `metadata.py`. | Ninguna escritura se escapa, se toca mínimo el código compartido con Abraham, y es robusto ante futuras funciones nuevas. |
| D2 | Replicación **lógica** (reaplicar sentencias derivadas del oplog), no copia binaria del archivo salvo en el snapshot inicial. | Enviar el `.db` entero cada vez. | El incremental es barato (solo deltas); el snapshot binario solo se usa para ponerse al día desde cero. |
| D3 | Snapshot con la **API de backup online de SQLite** (`sqlite3.Connection.backup`). | Copiar el archivo con `cp`. | El backup online es consistente aunque haya escrituras concurrentes; no requiere parar el primary. |
| D4 | **Pull** desde el standby (`GET /ha/oplog?after=`), no push del primary. | Push primary → standby. | El primary no necesita conocer el estado del standby; el standby controla su propio ritmo y reanudación. El mismo request sirve de heartbeat (R4.3). |
| D5 | Época persistida en tabla `ha_state` dentro de la misma `metadata.db`. | Archivo aparte. | Una sola unidad de durabilidad; se incluye en el snapshot. |
| D6 | Rechazo en standby con **HTTP 503** + `{"error":"not_primary","primary_url":...}`. | 421/409. | Acordado con el usuario; 503 encaja con "servicio no disponible aquí" y es fácil de distinguir por el cliente. |
| D7 | **Fencing por época**: el nodo con época menor se degrada. Sin árbitro externo. | Raft/ZooKeeper. | Alcance de 2 nodos para la demo; se documenta la limitación (ver §10). |
| D8 | El oplog **no captura los UPDATE de `last_heartbeat`** de `nodes` (son ruido de alta frecuencia). | Replicar cada heartbeat. | Evita agotar la retención del oplog; el estado de liveness se reconstruye tras la promoción con el periodo de gracia (R9) y el re-registro de los DataNodes. Sí se replican INSERT/DELETE de `nodes` y los cambios de `url`/`status`. |
| D9 | Todos los endpoints `/ha/*` exigen **token de nodo**; `/ha/promote` exige además **rol admin** (operador del clúster) y respeta TLS como el resto del servidor. | `/ha/*` sin auth. | La sincronización y la promoción son operaciones privilegiadas del clúster; nunca deben quedar expuestas a clientes. |

## 3. Configuración (`DFSHA_HA_*`)

Nuevas variables, todas en `config.py`:

| Variable | Default | Descripción |
|----------|---------|-------------|
| `DFSHA_HA_ROLE` | `primary` | `primary` o `standby`. Rol **inicial**; puede cambiar en runtime por promoción/degradación. |
| `DFSHA_HA_PEER_URL` | — | URL del otro ControlNode. Vacío = HA desactivado. |
| `DFSHA_HA_PEER_CA` | hereda `DFSHA_NODE_CA` | CA para validar TLS del par. |
| `DFSHA_HA_HEARTBEAT_SECONDS` | `3` | Intervalo del ciclo de sync/heartbeat del standby. |
| `DFSHA_HA_TIMEOUT_SECONDS` | `10` | Sin respuesta del primary por este tiempo ⇒ caído (≥ 3× heartbeat). |
| `DFSHA_HA_GRACE_SECONDS` | `30` | Tras promoverse, no marca DataNodes offline por este tiempo (R9). |
| `DFSHA_HA_OPLOG_RETENTION` | `10000` | Máx. entradas de oplog antes de podar las ya confirmadas. |

**Tiempos en la demo (ajuste 1).** Los defaults de arriba (heartbeat 3s, timeout
10s, gracia 30s) son los de producción/por defecto. El `docker-compose.yml` de la
demo los baja a **`DFSHA_HA_HEARTBEAT_SECONDS=2`** y **`DFSHA_HA_TIMEOUT_SECONDS=6`**
para que el failover se vea rápido al matar el primary; la gracia se deja en 30s.

Validación en el arranque: `DFSHA_HA_ROLE` solo acepta `primary`/`standby`; si no,
`run.py`/`config.py` aborta con mensaje claro (R1.5).

`config.py` expone además un objeto de estado mutable en runtime (no solo
constantes), porque el rol y la época cambian con la promoción. Se encapsula en un
nuevo módulo `dfsha/server/ha.py` (ver §5) para no ensuciar `config.py`.

## 4. Modelo de datos nuevo

Todo dentro de `metadata.db` (así entra en el snapshot y es durable).

### 4.1 Tabla `oplog`

```sql
CREATE TABLE IF NOT EXISTS oplog (
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    tbl      TEXT NOT NULL,
    op       TEXT NOT NULL CHECK (op IN ('INSERT','UPDATE','DELETE')),
    pk       TEXT NOT NULL,     -- clave primaria serializada (JSON)
    payload  TEXT               -- fila completa en JSON para INSERT/UPDATE; NULL en DELETE
);
```

`seq` es la secuencia monótona (R2.1). `payload` lleva la fila nueva completa; así
el standby reaplica con un `INSERT OR REPLACE` idempotente (o `DELETE` por `pk`),
sin tener que interpretar la sentencia SQL original.

### 4.2 Tabla `ha_state`

```sql
CREATE TABLE IF NOT EXISTS ha_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    epoch           INTEGER NOT NULL DEFAULT 0,
    applied_seq     INTEGER NOT NULL DEFAULT 0,  -- última seq de oplog aplicada (standby)
    role            TEXT NOT NULL DEFAULT 'primary'
);
```

Fila única (`id=1`). En el standby, `applied_seq` persiste el avance (R3.3). La
época (R5.3) vive aquí.

### 4.3 Triggers de captura

Por cada tabla replicada se crean tres triggers (INSERT/UPDATE/DELETE) que
insertan en `oplog`. Las tablas replicadas y su PK:

| Tabla | PK para `pk` |
|-------|--------------|
| `users` | `id` |
| `directories` | `id` |
| `files` | `id` |
| `blocks` | `id` |
| `api_keys` | `id` |
| `groups` | `id` |
| `group_members` | `(group_id,user_id)` |
| `acl` | `id` |
| `locks` | `(owner_id,path)` |
| `nodes` | `id` |
| `block_locations` | `(block_id,node_id)` |

Ejemplo (tabla `files`, se genera igual para las demás vía bucle en Python):

```sql
CREATE TRIGGER IF NOT EXISTS oplog_files_ai AFTER INSERT ON files BEGIN
  INSERT INTO oplog(ts,tbl,op,pk,payload) VALUES (
    (julianday('now')-2440587.5)*86400.0, 'files', 'INSERT',
    json_object('id', NEW.id),
    json_object('id',NEW.id,'user_id',NEW.user_id,'directory_id',NEW.directory_id,
                'name',NEW.name,'size',NEW.size,'created_at',NEW.created_at,
                'checksum',NEW.checksum));
END;
-- _au (AFTER UPDATE) análogo con NEW; _ad (AFTER DELETE) con OLD y payload NULL.
```

**Caso especial `nodes` — no replicar `last_heartbeat` (D8).** El trigger de
UPDATE sobre `nodes` solo escribe en el oplog cuando cambia algo distinto de
`last_heartbeat` (p. ej. `url` o `status`). Se consigue con la guarda
`WHEN NEW.url IS NOT OLD.url OR NEW.status IS NOT OLD.status`, de modo que un
heartbeat (que solo mueve `last_heartbeat`) **no genera entrada de oplog**. Los
INSERT y DELETE de `nodes` sí se capturan. Consecuencia: el standby conocerá qué
DataNodes existen y su `url`/`status`, pero su `last_heartbeat` puede quedar viejo;
por eso al promover entra en juego el **periodo de gracia** (R9) y el re-registro
de los DataNodes para refrescar el liveness.

**No duplicar oplog en el standby (ajuste 6).** Los triggers de captura existen
**solo en el nodo que actúa como primary**:

- Un nodo que arranca como `primary` crea los triggers en `initialize()`.
- Un nodo que arranca como `standby` **no** crea triggers; al reaplicar filas del
  oplog del primary no debe generar un segundo oplog propio (sería recursivo y
  rompería la secuencia). Si tras cargar un snapshot vinieran triggers del
  primary dentro del `.db`, se eliminan con `drop_oplog_triggers(db)`.
- Cuando el standby **se promueve** (`promote()`), lo primero que hace es
  `install_oplog_triggers(db)`, de modo que **a partir de la promoción** sus
  escrituras sí alimentan el oplog para un eventual nuevo standby (p. ej. el
  primary viejo cuando vuelve y se degrada).
- Cuando un primary viejo **se degrada** (`demote()`), ejecuta
  `drop_oplog_triggers(db)` antes de empezar a reaplicar como standby.

El rol, por tanto, es la única condición que determina la presencia de triggers,
y la transición de rol los instala/elimina de forma atómica.

> Nota de mantenimiento: la lista de columnas por tabla se deriva de
> `PRAGMA table_info(<tabla>)` al crear los triggers, para no desincronizarse si
> Abraham agrega columnas. Así el generador de triggers es una sola función.

### 4.4 Poda del oplog (R2.4b)

Cuando `oplog` supera `DFSHA_HA_OPLOG_RETENTION`, el primary borra las entradas
con `seq <= min(applied_seq confirmada por el standby)`. El standby confirma su
`applied_seq` como parámetro en el pull (`GET /ha/oplog?after=&ack=`). Si no hay
standby conectado, se conservan hasta el límite y luego se podan las más viejas
(forzando que un standby atrasado haga snapshot en vez de incremental — R3.1).

## 5. Módulo `dfsha/server/ha.py`

Nuevo módulo que concentra el estado y la lógica de HA, para no dispersar el
cambio por todo `main.py`.

Responsabilidades:

- **Estado en runtime**: `role`, `epoch`, `peer_url`, `promoted_at` (para el
  periodo de gracia), `last_peer_ok` (para `/ha/status`).
- **Carga/persistencia** de `ha_state` desde/hacia `metadata.db`.
- **Generación de triggers** y helpers de snapshot y de reaplicación de oplog.
- **Hilo de fondo del standby** (`replicator`): bucle que cada
  `DFSHA_HA_HEARTBEAT_SECONDS`:
  1. Pide `GET /ha/oplog?after=<applied_seq>&ack=<applied_seq>` al primary.
  2. Si el primary responde con `oldest_seq > applied_seq+1` (hueco por poda),
     dispara snapshot (`GET /ha/snapshot`) y reinicia `applied_seq`.
  3. Reaplica entradas en orden dentro de una transacción y actualiza `applied_seq`.
  4. Si el primary no responde durante `DFSHA_HA_TIMEOUT_SECONDS` ⇒ **promoción**.
  5. Si recibe del primary una época mayor que la propia ⇒ ya está al día; si la
     propia fuera mayor (caso raro), el primary se degradaría (ver §7).
- **Promoción** (`promote()`): incrementa época, cambia `role` a `primary`,
  deja de crear oplog-consumer, crea los triggers de captura, registra
  `promoted_at = now()` para el periodo de gracia, detiene el hilo replicador.
- **Degradación** (`demote(new_epoch)`): cambia `role` a `standby`, adopta la
  época mayor, arranca el hilo replicador apuntando al nuevo primary.

El hilo se arranca en el evento `startup` de FastAPI solo si el rol inicial es
`standby` y hay `peer_url` (igual que el heartbeat del DataNode ya existente).

## 6. Endpoints nuevos

Todos en `main.py` y bajo el mismo TLS del servidor. **Todos los `/ha/*`
requieren autenticación** (ajuste 3): token de nodo del clúster
(`cluster_auth.verify_node_token`, firmado con `DFSHA_CLUSTER_SECRET`), nunca
login de usuario normal.

| Método | Ruta | Auth | Descripción |
|--------|------|------|-------------|
| GET | `/ha/status` | token de nodo | Rol, época, `applied_seq`, `peer_url`, `peer_alive`, `grace_until`, ts. (R10) |
| GET | `/ha/oplog?after=&ack=` | token de nodo | Entradas con `seq > after`; incluye `epoch` y `oldest_seq` disponibles. Sirve de heartbeat. |
| GET | `/ha/snapshot` | token de nodo | Devuelve el `.db` consistente (backup online) como `application/octet-stream`. |
| POST | `/ha/promote` | token de nodo **+ admin** | Promoción manual asistida (R5.2). Exige además rol admin (operador), ver abajo. |

Detalles de autenticación (ajuste 3):

- El **token de nodo** se emite con `cluster_auth.issue_node_token`. El standby
  conoce `DFSHA_CLUSTER_SECRET` (idéntico en ambos ControlNodes, §9.5), así que
  puede firmar los tokens para pedir oplog/snapshot/status. Se reutiliza el mismo
  mecanismo que ya usan los DataNodes para `/nodes/register`.
- `/ha/promote` es una acción de **operador**, no de sincronización automática
  (esa va por timeout, internamente, sin pasar por el endpoint). Por eso exige,
  además del token de nodo, autorización de **admin del clúster**: un token de
  nodo marcado como admin, emitido por quien posee `DFSHA_CLUSTER_SECRET`. Se
  añade un helper `issue_admin_token`/`verify_admin_token` en `cluster_auth.py`
  (un claim `adm: true` en el token de nodo) para distinguir operador de un nodo
  cualquiera.
- `/ha/status` también exige token de nodo: no expone secretos, pero sí topología
  interna, así que no queda abierto. En la demo el script firma un token con el
  secreto del clúster para consultarlo.
- Todos respetan el TLS configurado con `DFSHA_TLS_*`, igual que el resto de la
  API; en la demo con compose van sobre la misma CA del clúster.

`/ha/oplog` y `/ha/snapshot` solo responden si el nodo se considera primary
(época vigente); si el llamante trae época mayor, el nodo se degrada.

## 7. Protocolo de época / anti split-brain

- La época es un entero en `ha_state`. Arranca en 0.
- **Promoción** (auto o manual): `epoch += 1`, `role=primary`. Como solo hay 2
  nodos, la época nueva siempre supera a la del primary viejo.
- Cada mensaje entre ControlNodes (`/ha/oplog`, `/ha/snapshot`, `/ha/promote`,
  y un pequeño anuncio en el pull) incluye la **época del emisor**.
- **Regla de fencing** (R5.4–R5.6): cuando un ControlNode recibe de su par una
  época **mayor** que la propia, se **degrada a standby** y adopta esa época. Un
  primary viejo que vuelve descubre la época mayor en su primer contacto y se
  rinde; mientras tanto, como el cliente/DataNode ya recibieron 503 del viejo o
  simplemente dejaron de alcanzarlo, no hay dos primaries sirviendo escrituras de
  forma estable.
- **Límite honesto** (sin árbitro): en una **partición de red** pura donde ambos
  se ven vivos para los clientes pero no entre sí, el standby podría promoverse y
  habría dos primaries hasta que la red se restablezca. Con 2 nodos y sin quórum
  esto es inevitable; se documenta como limitación (§10) y se mitiga con un
  timeout holgado. Es la misma clase de limitación que tiene un NameNode HA sin
  ZooKeeper/journal quorum.

## 8. Rechazo en el standby (R6, D6)

Se añade una dependencia FastAPI `require_primary` que se aplica a los endpoints
de cara al cliente (todo `/auth/*` de escritura, `/fs/*`, `/files/*`, `/acl/*`,
`/groups/*`, `/blocks/*` servidos por el ControlNode). Si el rol es `standby`:

```json
HTTP/1.1 503 Service Unavailable
Content-Type: application/json
{"error": "not_primary", "primary_url": "https://controlnode-a:8000"}
```

`primary_url` = `DFSHA_HA_PEER_URL` conocido (puede ser `null` si no se conoce).
No se tocan los endpoints de sincronización, `/ha/*` ni `/health`.

Implementación: en vez de decorar decenas de rutas a mano, se agrupa con un
pequeño middleware/dependencia que mira el path contra una lista blanca de
prefijos permitidos en standby (`/ha`, `/health`, `/docs`, `/openapi.json`) y
rechaza el resto cuando `role == standby`. Esto mantiene el cambio localizado.

## 9. Cambios en componentes existentes

### 9.1 `metadata.py`
- `initialize()`: crea `oplog`, `ha_state` (fila id=1), y —solo si el rol es
  primary— los triggers de captura. Nueva función `install_oplog_triggers(db)` y
  `drop_oplog_triggers(db)`.
- Helpers: `append_only` no hace falta (lo hacen los triggers). Se agregan
  `read_oplog(db, after, limit)`, `apply_oplog_entry(db, entry)`,
  `prune_oplog(db, upto)`, `get_ha_state(db)`, `set_ha_state(db, **cols)`.
- La creación de triggers usa `PRAGMA table_info` para derivar columnas (D1 nota).

### 9.2 `main.py`
- Monta el hilo replicador en `startup` si corresponde.
- Añade los endpoints `/ha/*`.
- Añade la barrera `require_primary` para el resto de rutas.
- En las rutas que listan/usan DataNodes (`available_nodes`), respeta el
  **periodo de gracia** tras promoción: si `now < grace_until`, no excluye nodos
  por `last_heartbeat` viejo (R9). Esto se implementa pasando `grace_until` a
  `_available_data_nodes()` / `metadata.available_nodes`.

### 9.3 `datanode.py` + `config.py` (R8, cambio mínimo)
- `config.py`: `CONTROL_URLS = [...]` derivado de `DFSHA_CONTROL_URL` (una sola,
  retrocompatible) **o** de una nueva `DFSHA_CONTROL_URLS` (lista separada por
  comas). Si solo está la vieja, la lista tiene un elemento. **No se elimina
  `DFSHA_CONTROL_URL`.**
- `datanode.py`: `_register_and_heartbeat` recorre `CONTROL_URLS`; usa la que
  responda. Si recibe 503 `not_primary` o un error de conexión, pasa a la
  siguiente. Guarda la última buena como preferida. Cambio contenido en esa
  función; el resto del DataNode queda igual.

### 9.4 Cliente (`client/api.py`, `client/cli.py`) (R7)
- `DfsClient.__init__` acepta `base_url` como str o lista; internamente guarda
  `self.urls`. `cli.py` permite múltiples `--server` (o una lista separada por
  comas) manteniendo el uso con una sola URL.
- `DfsClient.request`: si la petición falla por conexión/timeout o responde 503
  con `error == not_primary`, reintenta contra las otras URLs; al acertar, fija
  esa como `base_url` actual (R7.3). Si todas fallan, `DfsError` claro (R7.5).
- El contrato de descarga por bloques no cambia: las URLs de bloque siguen
  viniendo del plan `/files/blocks` del primary vigente (R7.4). Nota: los tickets
  de bloque siguen firmados con `DFSHA_CLUSTER_SECRET`, que es igual en ambos
  ControlNodes, así que un plan emitido justo antes del failover sigue siendo
  válido en los DataNodes.

### 9.5 Secretos idénticos en ambos ControlNodes (ajuste 4)

Para que el failover sea transparente, los dos ControlNodes DEBEN compartir los
mismos secretos, y DEBEN pasarse por **variables de entorno** (no por archivos
autogenerados distintos en cada contenedor):

- `DFSHA_CLUSTER_SECRET`: firma tickets de bloque y tokens de nodo, y ahora
  también los tokens de los endpoints `/ha/*`. Si difiere entre ControlNodes, el
  standby no podría pedir oplog/snapshot ni los tickets emitidos antes del
  failover seguirían siendo válidos.
- `DFSHA_SECRET`: firma los JWT de usuario. Debe ser el mismo para que un token
  de usuario emitido por el primary siga siendo válido en el nodo promovido (el
  cliente no tiene que volver a loguearse tras el failover).
- `DFSHA_STORAGE_KEY`: llave AES-256 de los bloques. Aunque el ControlNode en
  modo distribuido no cifra bloques (lo hacen los DataNodes), se mantiene idéntica
  en todo el clúster por coherencia y para el modo monolítico; **debe** compartirse
  igual que las otras.

Esto se refleja en el `docker-compose.yml` de la demo: ambos ControlNodes reciben
las tres variables desde el mismo `.env` (valores de ejemplo solo para desarrollo),
y se documenta en el README que en producción deben venir de un gestor de secretos.
Si un secreto se dejara autogenerar por nodo (archivo en `DFSHA_HOME`), cada
contenedor tendría uno distinto y el failover fallaría; por eso en HA es
**obligatorio** pasarlos por entorno.

## 10. Limitaciones conocidas (para el informe)

1. **Dos nodos, sin árbitro/quórum**: una partición de red simétrica puede causar
   doble primary transitorio (split-brain) hasta reconectar. Mitigado con timeout
   holgado; resuelto correctamente sería con un tercer árbitro (Raft/ZK) — fuera
   de alcance.
2. **Ventana de pérdida** = hasta `DFSHA_HA_HEARTBEAT_SECONDS` de oplog no
   replicado si el primary cae justo tras confirmar una escritura al cliente. Es
   replicación **asíncrona**; se documenta el valor.
3. **No hay re-sincronización automática del primary viejo** más allá de volverse
   standby: adopta la época mayor y hace snapshot del nuevo primary. Esto es
   suficiente para la demo.
4. La replicación de **bloques** (RF3) no entra aquí; si un DataNode único cae,
   sus bloques no están disponibles hasta las tareas 1–3.

## 11. Estrategia de pruebas (mapa a R12)

Framework: `pytest`, estilo de `tests/` actual (servidor real por proceso o app
en memoria con `TestClient`). Sin Docker.

| Prueba | Requisito | Qué verifica |
|--------|-----------|--------------|
| `test_oplog_captura_cambios` | R2 | Un INSERT/UPDATE/DELETE en metadatos crea entradas de oplog con seq creciente. |
| `test_sync_snapshot_inicial` | R3.1 | Standby arranca contra un primary con datos y queda idéntico tras snapshot. |
| `test_sync_incremental` | R3.2 | Cambios posteriores en el primary aparecen en el standby vía oplog. |
| `test_standby_rechaza_cliente` | R6 | Petición de cliente al standby ⇒ 503 `{"error":"not_primary",...}`. |
| `test_promocion_manual` | R5.2 | `POST /ha/promote` sube época y el nodo pasa a atender clientes. |
| `test_promocion_por_timeout` | R4,R5.1 | Primary "caído" ⇒ el standby se autopromueve. |
| `test_fencing_primary_viejo` | R5.5 | Primary viejo (época menor) que contacta al nuevo ⇒ se degrada a standby y no sirve escrituras. |
| `test_cliente_failover` | R7 | Cliente con 2 URLs: cae la primera ⇒ reintenta y acierta en la segunda. |
| `test_gracia_datanodes` | R9 | Tras promoción, un DataNode con heartbeat viejo no se excluye durante la gracia. |
| `test_control_urls_retrocompat` | R8.1 | `DFSHA_CONTROL_URL` sola sigue produciendo `CONTROL_URLS` de 1 elemento. |
| `test_heartbeat_no_genera_oplog` | R2.4b, D8 | Un UPDATE de solo `last_heartbeat` en `nodes` no agrega entradas al oplog; un cambio de `url`/`status` sí. |
| `test_ha_endpoints_requieren_auth` | R3.5, ajuste 3 | `/ha/oplog`, `/ha/snapshot`, `/ha/status`, `/ha/promote` sin token de nodo válido ⇒ rechazo; `promote` sin admin ⇒ rechazo. |

## 12. Orden de implementación sugerido (se detalla en tasks.md)

1. Config `DFSHA_HA_*` + validación + `CONTROL_URLS` retrocompatible.
2. Esquema: `oplog`, `ha_state`, generador de triggers y helpers en `metadata.py`.
3. Módulo `ha.py`: estado, snapshot, reaplicación, promoción/degradación.
4. Endpoints `/ha/*` + barrera `require_primary` + periodo de gracia.
5. Hilo replicador del standby + detección de caída + fencing por época.
6. Failover en `datanode.py` y en el cliente.
7. Docker Compose primary + standby.
8. Pruebas pytest.
9. Documentación (README + `docs/alta-disponibilidad-controlnode.md`).

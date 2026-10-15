# Plan de implementación — ControlNode de respaldo (HA)

Cada tarea es un paso incremental y verificable sobre la rama
`feature/controlnode-ha`. El orden permite probar unidades antes de integrar.
Cada tarea mayor termina en un commit pequeño con mensaje en español. No se hace
push ni merge sin aprobación.

Convención: `[ ]` pendiente. Las referencias "R#" apuntan a `requirements.md`.

- [x] 1. Configuración de HA y lista de ControlNodes (base, sin lógica aún)
  - [x] 1.1 Añadir en `config.py` las variables `DFSHA_HA_*` (ROLE, PEER_URL,
    PEER_CA, HEARTBEAT_SECONDS=3, TIMEOUT_SECONDS=10, GRACE_SECONDS=30,
    OPLOG_RETENTION=10000) con sus defaults y validación de `DFSHA_HA_ROLE`
    (solo `primary`/`standby`, default `primary`; valor inválido ⇒ `ValueError`).
  - [x] 1.2 Derivar `CONTROL_URLS` a partir de `DFSHA_CONTROL_URLS` (lista por
    comas) o, si no está, de `DFSHA_CONTROL_URL` (una sola, retrocompatible);
    conservar `DFSHA_CONTROL_URL` intacta.
  - [x] 1.3 Prueba unitaria `test_control_urls_retrocompat` y una de validación de
    `DFSHA_HA_ROLE`.
  - _Requisitos: R1, R8.1_
  - _Commit: `feat(ha): configuración DFSHA_HA_* y lista de ControlNodes`_

- [x] 2. Esquema de oplog y estado de HA en SQLite
  - [x] 2.1 Añadir a `SCHEMA` en `metadata.py` las tablas `oplog` y `ha_state`
    (fila única id=1, epoch, applied_seq, role) y crear la fila inicial en
    `initialize()` si no existe.
  - [x] 2.2 Implementar generador de triggers `install_oplog_triggers(db)` que, por
    cada tabla replicada (users, directories, files, blocks, api_keys, groups,
    group_members, acl, locks, nodes, block_locations), cree los triggers
    AFTER INSERT/UPDATE/DELETE derivando columnas con `PRAGMA table_info`.
  - [x] 2.3 Caso especial `nodes`: el trigger de UPDATE lleva guarda
    `WHEN NEW.url IS NOT OLD.url OR NEW.status IS NOT OLD.status` para no capturar
    los heartbeats (solo `last_heartbeat`). INSERT/DELETE sí se capturan.
  - [x] 2.4 Implementar `drop_oplog_triggers(db)`.
  - [x] 2.5 Helpers: `read_oplog(db, after, limit)`, `apply_oplog_entry(db, entry)`
    (INSERT OR REPLACE con payload / DELETE por pk, idempotente),
    `prune_oplog(db, upto)`, `get_ha_state(db)`, `set_ha_state(db, **cols)`.
  - [x] 2.6 En `initialize()`, instalar triggers solo si el rol inicial es
    `primary`; en `standby` no instalarlos (y dropearlos si existieran).
  - [x] 2.7 Pruebas: `test_oplog_captura_cambios` (seq creciente para I/U/D) y
    `test_heartbeat_no_genera_oplog`.
  - _Requisitos: R2, R2.4b, D8_
  - _Commit: `feat(ha): oplog por triggers de SQLite y tabla ha_state`_

- [x] 3. Módulo `dfsha/server/ha.py` (estado y operaciones, sin red todavía)
  - [x] 3.1 Estado en runtime (role, epoch, peer_url, promoted_at, last_peer_ok,
    applied_seq) cargado/persistido desde `ha_state`.
  - [x] 3.2 `snapshot_bytes()` usando la API de backup online de SQLite
    (`sqlite3.Connection.backup`) para producir un `.db` consistente.
  - [x] 3.3 `load_snapshot(raw)`: reemplazar la base local por el snapshot y
    dejar `applied_seq`/epoch coherentes; dropear triggers tras cargar.
  - [x] 3.4 `apply_entries(entries)`: reaplicar en orden dentro de una transacción
    y avanzar `applied_seq`.
  - [x] 3.5 `promote()`: `epoch += 1`, role=primary, `install_oplog_triggers`,
    fijar `promoted_at=now()`, persistir estado.
  - [x] 3.6 `demote(new_epoch)`: role=standby, adoptar epoch, `drop_oplog_triggers`.
  - [x] 3.7 `grace_until` y helper `in_grace()` para R9.
  - [x] 3.8 Pruebas unitarias de promote/demote (cambios de epoch/role y presencia
    de triggers).
  - _Requisitos: R3.1-3.3, R5.1-5.3, R9, ajuste 6_
  - _Commit: `feat(ha): módulo ha.py con snapshot, promoción y degradación`_

- [x] 4. Autenticación de operador en `cluster_auth.py`
  - [x] 4.1 Añadir claim admin (`adm: true`) con `issue_admin_token(secret)` y
    `verify_admin_token(secret, token)` reutilizando el token de nodo.
  - [x] 4.2 Prueba unitaria del token admin (válido / sin claim / firma mala).
  - _Requisitos: ajuste 3 (R5.2)_
  - _Commit: `feat(ha): token de operador para /ha/promote`_

- [x] 5. Endpoints `/ha/*` y barrera `require_primary`
  - [x] 5.1 `GET /ha/status` (token de nodo): rol, epoch, applied_seq, peer_url,
    peer_alive, grace_until, ts.
  - [x] 5.2 `GET /ha/oplog?after=&ack=` (token de nodo): entradas con seq>after,
    epoch del emisor y oldest_seq disponible; podar hasta `ack`. Solo responde si
    es primary; si el llamante trae epoch mayor, degradarse.
  - [x] 5.3 `GET /ha/snapshot` (token de nodo): `.db` por backup online como
    octet-stream.
  - [x] 5.4 `POST /ha/promote` (token de nodo + admin): promoción manual.
  - [x] 5.5 Barrera `require_primary`: cuando role=standby, responder 503
    `{"error":"not_primary","primary_url":...}` a todo salvo lista blanca
    (`/ha`, `/health`, `/docs`, `/openapi.json`).
  - [x] 5.6 Pruebas: `test_standby_rechaza_cliente`, `test_promocion_manual`,
    `test_ha_endpoints_requieren_auth`.
  - _Requisitos: R5.2, R6, R10, ajuste 3, D6_
  - _Commit: `feat(ha): endpoints /ha/* y rechazo 503 not_primary en standby`_

- [ ] 6. Hilo replicador del standby + detección de caída + fencing
  - [ ] 6.1 Hilo de fondo (patrón del heartbeat del DataNode) que cada
    `HEARTBEAT_SECONDS`: pide oplog al primary firmando token de nodo; aplica
    entradas; actualiza `last_peer_ok`.
  - [ ] 6.2 Si hay hueco por poda (`oldest_seq > applied_seq+1`), pedir snapshot.
  - [ ] 6.3 Si el primary no responde por `TIMEOUT_SECONDS` ⇒ `promote()`.
  - [ ] 6.4 Fencing: si el primary (u otro) anuncia epoch mayor, `demote()`.
  - [ ] 6.5 Arrancar el hilo en el evento `startup` solo si role inicial=standby y
    hay peer_url.
  - [ ] 6.6 Pruebas: `test_sync_snapshot_inicial`, `test_sync_incremental`,
    `test_promocion_por_timeout`, `test_fencing_primary_viejo`.
  - _Requisitos: R3, R4, R5.1, R5.4-5.6_
  - _Commit: `feat(ha): replicador del standby con failover y fencing por época`_

- [ ] 7. Periodo de gracia para DataNodes tras promoción
  - [ ] 7.1 Pasar `grace_until` a `_available_data_nodes()` / `available_nodes`
    para no excluir DataNodes por heartbeat viejo durante la gracia (R9.1).
  - [ ] 7.2 Asegurar que `/nodes/register` y `/nodes/{id}/heartbeat` del nuevo
    primary refrescan el estado durante la gracia (R9.2-9.3).
  - [ ] 7.3 Prueba `test_gracia_datanodes`.
  - _Requisitos: R9_
  - _Commit: `feat(ha): periodo de gracia de DataNodes tras promoción`_

- [ ] 8. Failover de DataNodes hacia el nuevo primary
  - [ ] 8.1 En `datanode.py`, `_register_and_heartbeat` recorre `CONTROL_URLS`;
    ante error de conexión o 503 `not_primary`, prueba la siguiente y recuerda la
    buena. Cambio contenido en esa función.
  - [ ] 8.2 Prueba de unidad del fallback de URLs del DataNode (puede ir en
    `test_distributed_units.py`).
  - _Requisitos: R8_
  - _Commit: `feat(ha): DataNode reintenta contra la lista de ControlNodes`_

- [ ] 9. Failover transparente en el cliente
  - [ ] 9.1 `DfsClient` acepta str o lista de URLs (`self.urls`), mantiene uso con
    una sola URL.
  - [ ] 9.2 En `request`, ante fallo de conexión/timeout o 503 `not_primary`,
    reintentar contra las otras URLs; al acertar, fijarla como actual.
  - [ ] 9.3 `cli.py`: aceptar varios `--server` o una lista separada por comas.
  - [ ] 9.4 Verificar que el contrato `/files/blocks` → `/blocks/{id}` no cambia.
  - [ ] 9.5 Prueba `test_cliente_failover`.
  - _Requisitos: R7_
  - _Commit: `feat(ha): cliente con lista de ControlNodes y reintento en failover`_

- [ ] 10. Docker Compose de demostración (primary + standby)
  - [ ] 10.1 Añadir servicios `controlnode-a` (primary) y `controlnode-b`
    (standby) con `DFSHA_HA_*` (demo: heartbeat 2s, timeout 6s, gracia 30s) y los
    tres secretos idénticos desde `.env` (`DFSHA_CLUSTER_SECRET`, `DFSHA_SECRET`,
    `DFSHA_STORAGE_KEY`).
  - [ ] 10.2 Apuntar los DataNodes a ambos ControlNodes vía `DFSHA_CONTROL_URLS`.
  - [ ] 10.3 Comentar en el compose qué valores son solo de desarrollo.
  - _Requisitos: R11, ajuste 1, ajuste 4_
  - _Commit: `feat(ha): docker-compose con ControlNode primary + standby`_

- [ ] 11. Documentación
  - [ ] 11.1 Actualizar `README.md`: variables `DFSHA_HA_*` y `DFSHA_CONTROL_URLS`,
    endpoints `/ha/*`, nota de secretos idénticos por entorno, y cómo correr la
    demo de failover.
  - [ ] 11.2 Crear `docs/alta-disponibilidad-controlnode.md`: diseño elegido,
    protocolos ControlNode↔ControlNode y cliente↔ControlNode, época/fencing,
    ventana de pérdida y limitaciones conocidas.
  - _Requisitos: R13_
  - _Commit: `docs(ha): README y documento de alta disponibilidad del ControlNode`_

- [ ] 12. Verificación final e integración
  - [ ] 12.1 Correr toda la suite `pytest -q` y arreglar regresiones.
  - [ ] 12.2 Repaso manual de la demo según R11.2 (matar primary; ls/get/put).
  - [ ] 12.3 Dejar la rama lista para Pull Request (sin push/merge hasta aprobación).
  - _Requisitos: R12, R11_
  - _Commit: `test(ha): suite completa de alta disponibilidad en verde`_

## Notas de ejecución

- Las pruebas no dependen de Docker (R12.6): usan `TestClient` de FastAPI y/o
  procesos locales como el resto de `tests/`.
- Para `test_promocion_por_timeout` se puede inyectar un peer_url inalcanzable o
  bajar el timeout a sub-segundo en la prueba, evitando esperas largas.
- No se toca la lógica interna del DataNode salvo el fallback de URLs (tarea 8).
- Orden de dependencias: 1→2→3 son la base; 4→5 exponen la API; 6 cierra el bucle
  de failover; 7-9 integran clúster y cliente; 10-12 demo, docs y cierre.

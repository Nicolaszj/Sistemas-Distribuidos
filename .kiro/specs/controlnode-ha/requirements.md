# Requisitos — ControlNode de respaldo (Alta disponibilidad)

Tarea 4 del hito 3 de DFSha. Objetivo: eliminar el punto único de falla del
ControlNode con un nodo de respaldo (standby) que replica los metadatos del
principal (primary) y toma el control si este cae, sin romper el contrato del
cliente ni la arquitectura de DataNodes del hito 2.

## Glosario

- **ControlNode primary**: nodo que atiende todas las peticiones de clientes y
  DataNodes y es dueño de la base de metadatos autoritativa.
- **ControlNode standby**: nodo que solo replica metadatos del primary y no
  atiende peticiones de escritura/lectura de clientes hasta ser promovido.
- **Oplog**: registro ordenado de operaciones de escritura sobre los metadatos,
  usado para la replicación incremental primary → standby.
- **Época (epoch / term)**: entero monótono creciente que identifica al "reinado"
  de un primary. Sirve de token de fencing contra split-brain.
- **Promoción**: transición de standby a primary.
- **Fencing**: mecanismo que impide que un primary viejo (con época menor) siga
  actuando como primary tras una partición o reinicio.

## Decisiones de diseño ya acordadas (entradas, no a debatir en requirements)

- Sincronización por **oplog** (log de operaciones) capturado con **triggers de
  SQLite** sobre una tabla `oplog`, con **snapshot inicial** de la base cuando el
  standby arranca o se queda muy atrás.
- Durante la ventana de detección + promoción, las **escrituras fallan con un
  error claro** y el cliente **reintenta** contra el otro ControlNode
  (consistencia por encima de disponibilidad).
- **Autopromoción** del standby al vencer el timeout de heartbeat entre
  ControlNodes, con **fencing por época**, más endpoint **manual**
  `POST /ha/promote`.
- Configuración por variables de entorno con prefijo `DFSHA_`.
- La lista de URLs de ControlNode en cliente y DataNode debe ser **compatible
  hacia atrás**: una sola `DFSHA_CONTROL_URL` (o un solo `--server`) debe seguir
  funcionando igual que hoy.

## Requisitos

### R1 — Rol configurable del ControlNode

**Historia:** Como operador del clúster, quiero declarar si un ControlNode
arranca como primary o como standby y quién es su par, para montar la pareja de
alta disponibilidad sin cambiar el código.

**Criterios de aceptación:**

1. CUANDO se define `DFSHA_HA_ROLE=primary` ENTONCES el nodo DEBE arrancar
   atendiendo todas las peticiones de clientes y DataNodes como hoy.
2. CUANDO se define `DFSHA_HA_ROLE=standby` ENTONCES el nodo DEBE arrancar en
   modo réplica: solo acepta sincronización desde el primary y endpoints de
   estado/HA.
3. CUANDO no se define `DFSHA_HA_ROLE` ENTONCES el nodo DEBE comportarse como
   `primary` (compatibilidad con el despliegue monolítico/actual).
4. Toda la configuración de HA DEBE unificarse bajo el prefijo `DFSHA_HA_*`
   (p. ej. `DFSHA_HA_ROLE`, `DFSHA_HA_PEER_URL`, `DFSHA_HA_HEARTBEAT_SECONDS`,
   `DFSHA_HA_TIMEOUT_SECONDS`, `DFSHA_HA_GRACE_SECONDS`), todas con valores por
   defecto razonables y `DFSHA_HA_ROLE` por defecto `primary`.
5. SI `DFSHA_HA_ROLE` tiene un valor distinto de `primary`/`standby` ENTONCES el
   arranque DEBE fallar con un mensaje de error claro.

### R2 — Registro de operaciones (oplog) en el primary

**Historia:** Como standby, necesito que el primary registre cada cambio de
metadatos en orden, para poder reproducirlos y mantener una copia consistente.

**Criterios de aceptación:**

1. CUANDO se aplica cualquier INSERT/UPDATE/DELETE sobre las tablas de metadatos
   replicadas ENTONCES el sistema DEBE registrar la operación en una tabla
   `oplog` con un número de secuencia monótono creciente.
2. La captura DEBE hacerse con **triggers de SQLite**, de modo que ninguna
   escritura hecha a través de `metadata.connect()` se escape del oplog, sin
   instrumentar manualmente cada función de `metadata.py`.
3. El oplog DEBE incluir, por entrada: secuencia, tabla, tipo de operación,
   clave/datos suficientes para reaplicar el cambio, y marca de tiempo.
4. Las tablas replicadas DEBEN incluir: `users`, `directories`, `files`,
   `blocks`, `api_keys`, `groups`, `group_members`, `acl`, `locks`, `nodes`,
   `block_locations`. Se replica también `locks` para que un lock vigente
   sobreviva al failover.
4b. El oplog DEBE poder podarse (descartar entradas ya consumidas y confirmadas
   por el standby) para que la tabla no crezca sin límite.

### R3 — Sincronización primary → standby

**Historia:** Como standby, quiero obtener una copia consistente de los
metadatos del primary y mantenerla al día con baja latencia.

**Criterios de aceptación:**

1. CUANDO el standby arranca o detecta que su secuencia quedó por detrás del
   punto más antiguo disponible en el oplog del primary ENTONCES DEBE tomar un
   **snapshot** completo y consistente de la base del primary y cargarlo.
2. DESPUÉS del snapshot (o si ya está al día) el standby DEBE pedir
   incrementalmente las entradas del oplog con secuencia mayor a la última
   aplicada y reaplicarlas en orden.
3. El standby DEBE exponer y persistir la **última secuencia aplicada** para
   poder reanudar sin volver a copiar todo.
4. La ventana de pérdida de datos esperada DEBE ser menor o igual al intervalo
   de sondeo del oplog (objetivo: ≤ unos pocos segundos) y DEBE documentarse.
5. Los endpoints de snapshot y de lectura del oplog DEBEN autenticarse con el
   secreto del clúster (token de nodo), nunca expuestos a clientes.
6. SI la sincronización falla temporalmente ENTONCES el standby DEBE reintentar
   sin caerse y DEBE reflejar el estado de atraso en su endpoint de estado.

### R4 — Detección de caída del primary (heartbeat entre ControlNodes)

**Historia:** Como standby, quiero detectar que el primary dejó de responder,
para poder promoverme.

**Criterios de aceptación:**

1. El standby DEBE sondear la salud del primary a intervalos
   `DFSHA_HA_HEARTBEAT_SECONDS` (valor por defecto configurable).
2. CUANDO el primary no responde durante `DFSHA_HA_TIMEOUT_SECONDS` seguidos
   ENTONCES el standby DEBE considerar al primary como caído.
3. El sondeo DEBE combinarse con el ciclo de sincronización del oplog para no
   duplicar conexiones innecesarias.
4. El timeout DEBE ser configurable y su valor por defecto DEBE evitar falsos
   positivos razonables (p. ej. timeout ≥ 3× intervalo).

### R5 — Promoción con fencing por época (anti split-brain)

**Historia:** Como clúster, quiero que al promoverse el standby incremente una
época y que el primary viejo no pueda seguir actuando como principal al volver,
para no tener dos primaries a la vez.

**Criterios de aceptación:**

1. CUANDO el standby confirma la caída del primary ENTONCES DEBE **autopromoverse**:
   incrementar la época, pasar a rol primary y empezar a atender peticiones.
2. El sistema DEBE exponer `POST /ha/promote` para promover manualmente el
   standby (promoción asistida), con la misma lógica de incremento de época.
3. La época DEBE persistirse de forma durable en cada nodo.
4. CUANDO un ControlNode recibe una petición de otro ControlNode (sincronización,
   anuncio) con una época mayor que la suya ENTONCES DEBE reconocer que quedó
   obsoleto y **degradarse a standby**.
5. CUANDO el primary viejo vuelve y descubre que existe una época mayor
   ENTONCES DEBE convertirse en standby del nuevo primary y NO DEBE seguir
   aceptando escrituras como primary (fencing).
6. El sistema NO DEBE quedar nunca con dos nodos creyéndose primary de la misma
   época de forma estable; cualquier conflicto se resuelve a favor de la época
   mayor.

### R6 — Rechazo de peticiones en el standby

**Historia:** Como cliente que por error apunta al standby, quiero una respuesta
clara para redirigir mi petición al primary.

**Criterios de aceptación:**

1. CUANDO un cliente envía una petición de API de archivos/datos a un nodo en
   rol standby ENTONCES el nodo DEBE responder con **HTTP 503** y cuerpo JSON
   `{"error":"not_primary","primary_url": <url conocida del primary o null>}`
   en vez de servirla.
2. La respuesta DEBE permitir al cliente identificar que debe reintentar en otro
   ControlNode a partir de `error == "not_primary"` y, si viene, de `primary_url`.
3. El standby SÍ DEBE responder a: endpoints de sincronización (con token de
   nodo), endpoint de estado y endpoints de HA.
4. El rechazo NO DEBE revelar datos de metadatos a un cliente no autorizado.

### R7 — Failover transparente para el cliente

**Historia:** Como usuario del CLI/SDK, quiero que `ls`, `get` y `put` sigan
funcionando tras caer el primary, sin tener que reconfigurar nada manualmente.

**Criterios de aceptación:**

1. El cliente DEBE aceptar **una lista** de URLs de ControlNode (varios
   `--server` o separados por coma) manteniendo compatibilidad con una sola URL.
2. CUANDO la petición al ControlNode actual falla por conexión/timeout o por el
   error "no soy el primary" ENTONCES el cliente DEBE reintentar automáticamente
   contra las otras URLs antes de dar error al usuario.
3. CUANDO un reintento tiene éxito contra otra URL ENTONCES el cliente DEBE
   recordar ese nodo como el actual para las siguientes peticiones.
4. El contrato de descarga existente (`GET /files/blocks` → `GET /blocks/{id}`)
   NO DEBE cambiar de cara al cliente.
5. SI todas las URLs fallan ENTONCES el cliente DEBE dar un error claro que
   mencione que ningún ControlNode respondió.

### R8 — Failover de los DataNodes hacia el nuevo primary

**Historia:** Como DataNode, quiero reportarme al ControlNode que esté activo,
para que tras una promoción el nuevo primary conozca mi inventario de bloques.

**Criterios de aceptación:**

1. El DataNode DEBE aceptar una lista de URLs de ControlNode manteniendo
   compatibilidad total con una sola `DFSHA_CONTROL_URL` (el cambio debe ser
   mínimo y no romper el comportamiento actual).
2. CUANDO el registro/heartbeat contra un ControlNode falla o es rechazado por
   no ser el primary ENTONCES el DataNode DEBE intentar con las otras URLs.
3. DESPUÉS de una promoción, el nuevo primary DEBE poder recibir el
   registro/heartbeat de los DataNodes y reconstruir/confirmar su estado online.

### R9 — Periodo de gracia tras la promoción

**Historia:** Como nuevo primary recién promovido, no quiero marcar como caídos
a DataNodes que en realidad están vivos solo porque el `last_heartbeat`
replicado quedó viejo.

**Criterios de aceptación:**

1. CUANDO un nodo se promueve a primary ENTONCES DEBE aplicar un **periodo de
   gracia** configurable (`DFSHA_HA_GRACE_SECONDS`) durante el cual NO marca
   DataNodes como offline por heartbeat viejo.
2. DURANTE el periodo de gracia el nuevo primary DEBE aceptar y procesar los
   re-registros de los DataNodes para refrescar su `last_heartbeat`.
3. DESPUÉS del periodo de gracia DEBE aplicarse la regla normal de timeout de
   DataNodes.

### R10 — Endpoint de estado de HA

**Historia:** Como operador y para la demo, quiero consultar el estado de HA de
un nodo (rol, época, atraso de sincronización, par) para depurar y mostrar el
failover.

**Criterios de aceptación:**

1. El sistema DEBE exponer `GET /ha/status` (sin requerir login de usuario) que
   devuelva al menos: rol actual, época, última secuencia de oplog aplicada,
   URL del par, si considera al par vivo, y marca de tiempo.
2. El endpoint DEBE funcionar tanto en primary como en standby.
3. El endpoint NO DEBE revelar secretos ni metadatos de usuarios.

### R11 — Despliegue de demostración (Docker Compose)

**Historia:** Como estudiante presentando el proyecto, quiero levantar una pareja
primary + standby con Docker Compose y demostrar el failover.

**Criterios de aceptación:**

1. DEBE existir una topología de Compose con ControlNode primary + ControlNode
   standby (y los DataNodes existentes) que arranque con un solo comando.
2. CUANDO se detiene el contenedor del primary ENTONCES, tras el timeout, el
   standby DEBE promoverse y `ls`, `get` y `put` DEBEN seguir funcionando
   usando la lista de URLs del cliente.
3. La configuración de ejemplo DEBE dejar claros los valores que son solo para
   desarrollo (secretos) frente a producción.

### R12 — Pruebas automáticas (pytest)

**Historia:** Como responsable de esta rama, quiero pruebas que respalden cada
propiedad clave de HA para el PR y el informe.

**Criterios de aceptación:**

1. DEBE haber pruebas de: sincronización de metadatos primary → standby
   (incluye snapshot inicial e incremental por oplog).
2. DEBE haber una prueba de rechazo de escrituras/lecturas de cliente en el
   standby con el error acordado.
3. DEBE haber una prueba de promoción (manual vía `POST /ha/promote` y/o por
   timeout) que verifique el incremento de época y que el nuevo primary atiende.
4. DEBE haber una prueba de fencing: un primary viejo que vuelve con época menor
   se degrada a standby y no acepta escrituras como primary.
5. DEBE haber una prueba del failover del cliente (reintento contra la otra URL).
6. Las pruebas NO DEBEN depender de Docker; deben poder correr con procesos o
   apps en memoria como el resto de `tests/`.

### R13 — Documentación

**Historia:** Como autor del informe técnico, quiero la documentación del diseño
y la operación de la HA del ControlNode.

**Criterios de aceptación:**

1. El `README.md` DEBE actualizarse con: nuevas variables `DFSHA_*`, los nuevos
   endpoints (`/ha/status`, `/ha/promote`, sincronización) y cómo correr la demo.
2. DEBE crearse `docs/alta-disponibilidad-controlnode.md` con el diseño elegido,
   los protocolos ControlNode ↔ ControlNode y cliente ↔ ControlNode, el manejo
   de época/fencing y las limitaciones conocidas.
3. La documentación DEBE indicar explícitamente la ventana de pérdida de datos
   aceptada y los supuestos (p. ej. sin árbitro externo, failover de dos nodos).

## Fuera de alcance (de esta rama)

- Replicación de **bloques** con factor 3 y re-replicación (tareas 1–3; dependen
  de los DataNodes y se harán en ramas aparte).
- Lectura de bloques con failover entre réplicas (tarea 3).
- Clústeres de más de dos ControlNodes o consenso tipo Raft/ZooKeeper con árbitro
  externo (se menciona como limitación/trabajo futuro).

## Supuestos de integración con los DataNodes (Abraham)

- La rama `datanode-architecture` ya está integrada en `main`; se parte de ahí.
- No se modifica la lógica interna del DataNode salvo el mínimo necesario para
  aceptar una lista de URLs de ControlNode (R8), de forma retrocompatible.
- Mientras no exista RF3 de replicación de bloques, cada bloque vive en un solo
  DataNode; el standby solo replica metadatos (incluye `block_locations`).

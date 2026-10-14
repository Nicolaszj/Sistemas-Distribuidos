# DFSha — Sistema de archivos distribuido

Proyecto 1 de SI3007 Sistemas Distribuidos (2026-2). Opción 1: arquitectura cliente/servidor.

Estado actual: versión C/S con **RF1** (gestión del sistema de archivos), **RF2**
(transferencia), **RF3** (acceso: open/read/seek/write/close/lock) y la **capa de seguridad**
(TLS, cifrado en reposo, 2FA, API keys, ACL por usuario y grupo, tickets de bloque entre nodos).
El servidor cumple todavía el rol de ControlNode (metadatos) y de DataNode (bloques); en el hito 2
esos roles se separan en procesos distintos sin cambiar el contrato del cliente.

## Estructura

```
dfsha/
  paths.py            normalización de rutas (compartida cliente/servidor)
  cluster_auth.py     tickets de bloque y tokens de nodo (secreto del clúster)
  server/
    config.py         parámetros por variables de entorno y secretos persistentes
    auth.py           PBKDF2, JWT, API keys y bloqueo por fuerza bruta
    totp.py           segundo factor TOTP (RFC 6238)
    access.py         resolución de rutas /~usuario y verificación de ACL
    metadata.py       esquema y consultas SQLite (ControlNode)
    storage.py        bloques cifrados con AES-256-GCM en disco (DataNode)
    main.py           API REST (FastAPI)
    run.py            arranque del nodo, con TLS si hay certificados
  client/
    api.py            cliente HTTP de la API (verifica TLS y checksums)
    files.py          RF3: descriptor de archivo remoto (dfs_open)
    cli.py            shell interactivo
scripts/
  gen_certs.py        CA del clúster y certificados de cada nodo
tests/                pruebas automáticas (pytest) contra un servidor real con TLS
```

## Ejecución

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 1. Certificados (una vez). Agrega en --san la IP pública / DNS de la máquina.
python scripts/gen_certs.py controlnode --san 127.0.0.1

# 2. Servidor con TLS
#    Linux/macOS:
DFSHA_TLS_CERT=certs/controlnode.crt DFSHA_TLS_KEY=certs/controlnode.key python -m dfsha.server.run
#    Windows (PowerShell):
#    $env:DFSHA_TLS_CERT="certs/controlnode.crt"; $env:DFSHA_TLS_KEY="certs/controlnode.key"; python -m dfsha.server.run
```

Cliente, en otra terminal (solo necesita `certs/ca.crt`):

```bash
python -m dfsha.client.cli --server https://localhost:8000 --ca certs/ca.crt --user nico --register
python -m dfsha.client.cli --server https://localhost:8000 --ca certs/ca.crt --user nico
python -m dfsha.client.cli --server https://localhost:8000 --ca certs/ca.crt --api-key dfsha_...
```

Sin `DFSHA_TLS_CERT` el nodo arranca en http plano y lo advierte (solo para desarrollo).

Con Docker:

```bash
docker build -t dfsha .
docker run -p 8000:8000 -v dfsha-data:/data -v "$PWD/certs:/certs:ro" \
  -e DFSHA_TLS_CERT=/certs/controlnode.crt -e DFSHA_TLS_KEY=/certs/controlnode.key dfsha
```

La documentación interactiva de la API queda en `https://localhost:8000/docs`.

### Pruebas

```bash
pip install -r requirements-dev.txt
pytest -q
```

Levantan un servidor real con TLS y prueban cifrado, tickets, 2FA, API keys, ACL, grupos,
locks, lecturas por rango y checksums.

### Variables de entorno

| Variable | Valor por defecto | Descripción |
| --- | --- | --- |
| `DFSHA_HOME` | `./data` | Carpeta con la base de metadatos y los bloques |
| `DFSHA_BLOCK_SIZE` | `4194304` (4 MiB) | Tamaño de bloque en bytes |
| `DFSHA_PORT` | `8000` | Puerto del nodo (`run.py`) |
| `DFSHA_TLS_CERT` / `DFSHA_TLS_KEY` | — | Certificado y llave del nodo; activan https |
| `DFSHA_TLS_CA` | — | CA del clúster; con `DFSHA_TLS_REQUIRE_CLIENT_CERT=1` exige mTLS |
| `DFSHA_SECRET` | generado en `jwt.secret` | Llave para firmar los JWT de usuario |
| `DFSHA_CLUSTER_SECRET` | generado en `cluster.secret` | Secreto compartido entre nodos (tickets) |
| `DFSHA_STORAGE_KEY` | generada en `storage.key` | Llave AES-256 (base64) para cifrar bloques |
| `DFSHA_TOKEN_TTL` | `3600` | Vigencia del token en segundos |
| `DFSHA_BLOCK_TICKET_TTL` | `300` | Vigencia de los tickets de bloque |
| `DFSHA_LOCK_TTL` | `60` | Duración por defecto del lease de un lock |
| `DFSHA_LOGIN_MAX_FAILURES` | `5` | Intentos fallidos antes de bloquear el login |
| `DFSHA_LOGIN_BLOCK_SECONDS` | `60` | Duración del bloqueo |

Si un secreto no viene por variable de entorno se genera aleatorio la primera vez y se guarda en
`DFSHA_HOME`. En un clúster, `DFSHA_SECRET` y `DFSHA_CLUSTER_SECRET` deben ser **iguales en todos
los nodos** y pasarse por variable de entorno.

## Comandos del cliente

| Comando | Descripción |
| --- | --- |
| `ls [ruta]` | Lista un directorio |
| `cd <ruta>` | Cambia de directorio (soporta `.` y `..`) |
| `pwd` | Muestra el directorio actual |
| `mkdir <ruta>` | Crea un directorio |
| `rmdir <ruta>` | Borra un directorio vacío |
| `rm <ruta>` | Borra un archivo y sus bloques |
| `put <local> [remoto]` | Sube un archivo (RF2 `send`) |
| `get <remoto> [local]` | Descarga un archivo bloque por bloque (RF2 `receive`) |
| `blocks <ruta>` | Muestra en qué bloques quedó partido un archivo |
| `stat <ruta>` | Tipo, tamaño, SHA-256 y estado del lock |
| `cat <ruta>` | RF3: imprime el archivo |
| `read <ruta> <offset> <n>` | RF3: lee `n` bytes desde `offset`, transfiriendo solo ese rango |
| `write <ruta> <texto>` / `append <ruta> <texto>` | RF3: crea/reemplaza o agrega al final |
| `lock <ruta> [s]` / `unlock <ruta>` | RF3: lock exclusivo de escritura con lease |
| `share <ruta> <usuario\|@grupo> <r\|rw>` | Comparte un archivo o directorio |
| `unshare <ruta> <usuario\|@grupo>` | Revoca el permiso |
| `acl <ruta>` / `shared` | Quién tiene acceso / qué compartieron conmigo |
| `group new\|add\|del ...` / `groups` | Administra grupos |
| `2fa setup\|enable\|disable` | Segundo factor TOTP (muestra un QR en la terminal) |
| `apikey new\|ls\|rm` | API keys para scripts |
| `whoami` | Usuario, grupos y estado del 2FA |

Las rutas de otro usuario se escriben `/~usuario/ruta` (por ejemplo `ls /~ana/proyecto`).

Desde Python, RF3 se usa como un archivo normal:

```python
from dfsha.client.api import DfsClient
from dfsha.client.files import dfs_open

client = DfsClient("https://localhost:8000", ca_file="certs/ca.crt")
client.login("nico", "contraseña")

with dfs_open(client, "/datos/video.mp4") as f:   # lectura aleatoria por rangos
    f.seek(1_000_000)
    cabecera = f.read(64)

with dfs_open(client, "/datos/log.txt", "a") as f:  # toma el lock mientras está abierto
    f.write("nueva línea\n")
```
## API REST

Todas las rutas, salvo registro y login, requieren `Authorization: Bearer <token>` o
`X-API-Key: <llave>`. `/blocks/{id}` acepta en su lugar `X-Block-Ticket: <ticket>`.

| Método | Ruta | Descripción |
| --- | --- | --- |
| POST | `/auth/register` | Crea un usuario y su directorio raíz |
| POST | `/auth/login` | Devuelve un token JWT (`otp` si el 2FA está activo) |
| GET | `/auth/me` | Usuario, grupos y estado del 2FA |
| POST | `/auth/2fa/setup` · `/enable` · `/disable` | Configura el TOTP |
| POST · GET · DELETE | `/auth/apikeys[/{id}]` | Crea, lista y revoca API keys |
| POST · GET | `/groups` | Crea un grupo / lista mis grupos |
| POST · DELETE | `/groups/{nombre}/members[/{usuario}]` | Agrega o quita miembros |
| POST | `/acl/grant` · `/acl/revoke` | Da o quita `r`/`rw` a un usuario o grupo |
| GET | `/acl?path=` · `/acl/shared` | ACL de una ruta / lo compartido conmigo |
| POST | `/fs/lock` · `/fs/unlock` | Lock exclusivo con lease (423 si lo tiene otro) |
| GET | `/fs/ls?path=` | Lista directorios y archivos |
| GET | `/fs/stat?path=` | Tipo, tamaño y fecha de una ruta |
| POST | `/fs/mkdir` | Crea un directorio |
| POST | `/fs/rmdir` | Borra un directorio vacío |
| DELETE | `/fs/rm?path=` | Borra un archivo |
| POST | `/files/upload?path=[&overwrite=true]` | Sube (o reemplaza atómicamente) el archivo |
| GET | `/files/download?path=` | Devuelve el archivo reensamblado |
| GET | `/files/blocks?path=` | Plan de bloques: id, tamaño, SHA-256 y ticket firmado |
| GET | `/blocks/{id}[?offset=&length=]` | Contenido de un bloque o de un rango |

Ejemplo con `curl`:

```bash
TOKEN=$(curl -s -X POST localhost:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"nico","password":"secreta"}' | python -c 'import sys,json;print(json.load(sys.stdin)["token"])')

curl -X POST "localhost:8000/fs/mkdir" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"path":"/datos"}'

curl -X POST "localhost:8000/files/upload?path=/datos/video.mp4" \
  -H "Authorization: Bearer $TOKEN" --data-binary @video.mp4
```

## Modelo de datos

SQLite en `$DFSHA_HOME/metadata.db`:

- `users`: usuario, hash PBKDF2-SHA256, salt y secreto TOTP.
- `directories`: árbol por usuario (`parent_id` nulo en la raíz de cada usuario).
- `files`: nombre, tamaño, fecha y SHA-256 del archivo, únicos dentro de un directorio.
- `blocks`: identificador, archivo, secuencia, tamaño y SHA-256 de cada bloque.
- `api_keys`: hash SHA-256 de cada llave, nombre y último uso.
- `groups`, `group_members`: grupos de usuarios.
- `acl`: permisos `r`/`rw` de un usuario o grupo sobre un directorio (heredado) o archivo.
- `locks`: lock exclusivo por archivo con su vencimiento (lease).

Las columnas nuevas se agregan solas al arrancar sobre una base del hito 1.

Los bloques se guardan como archivos independientes en `$DFSHA_HOME/blocks/<2 primeros
caracteres del id>/<id>`, de modo que el directorio no crece plano y cada bloque puede migrar a
otro nodo en el hito 2.

## Particionamiento

La subida se lee en streaming: el servidor va acumulando el cuerpo de la petición y cada vez que
alcanza `DFSHA_BLOCK_SIZE` escribe un bloque nuevo con un identificador aleatorio; el último
bloque conserva el residuo. Los metadatos del archivo se registran cuando todos los bloques
están en disco, así una subida interrumpida no deja el archivo visible.

La descarga del cliente usa el camino inverso y ya está pensada para el modelo distribuido:
primero pide el plan de bloques al ControlNode (`/files/blocks`) y después descarga cada bloque
por separado (`/blocks/{id}`). En el hito 2 solo cambia de dónde viene cada bloque, porque el plan
incluirá la dirección del DataNode correspondiente.

## Seguridad

| Aspecto | Implementación |
| --- | --- |
| Cifrado en tránsito | TLS en todos los nodos (`run.py`), CA propia del clúster (`gen_certs.py`); el cliente valida el certificado con `--ca`. Opción mTLS entre nodos internos. |
| Cifrado en reposo | Cada bloque en disco va cifrado con AES-256-GCM; el id del bloque es dato asociado, así que un bloque alterado o intercambiado no se descifra. |
| Integridad extremo a extremo | SHA-256 por bloque y por archivo guardados en el ControlNode; el cliente los verifica al descargar. |
| Autenticación | Usuario/contraseña (PBKDF2-SHA256, 200.000 iteraciones, salt por usuario, mínimo 8 caracteres), JWT HS256 con vencimiento, 2FA TOTP opcional y API keys revocables (solo se guarda su hash). |
| Fuerza bruta | Tras `DFSHA_LOGIN_MAX_FAILURES` fallos seguidos el login responde 429 durante `DFSHA_LOGIN_BLOCK_SECONDS`. |
| Autorización | Cada usuario es dueño de su árbol; ACL `r`/`rw` por usuario o grupo, heredada por directorios. Sin permiso se responde 404 para no revelar rutas ajenas. |
| Seguridad entre nodos | `cluster_auth.py`: el ControlNode valida usuario y ACL y emite un ticket firmado y de corta vida por bloque; el DataNode solo valida el ticket (firma, bloque, operación, vencimiento), sin acceso a la base de metadatos. Los nodos se identifican entre sí con tokens firmados con `DFSHA_CLUSTER_SECRET`. |
| Secretos | Nunca en el código: variables de entorno o archivos generados con permisos 600. `certs/` y `*.key` están en `.gitignore`. |

## Pendiente

- Hito 2: separar ControlNode y DataNodes, registro y heartbeat de nodos, ubicación de bloques
  (los DataNodes usan `cluster_auth.verify_block_ticket` para autorizar).
- Hito 3: replicación de bloques, consistencia y alta disponibilidad del ControlNode.

# DFSha — Sistema de archivos distribuido

Proyecto 1 de SI3007 Sistemas Distribuidos (2026-2). Opción 1: arquitectura cliente/servidor.

Estado actual: **hito 1 (semana 8)** — versión monolítica C/S con RF1 (gestión del sistema de
archivos) y RF2 (transferencia de archivos) completos. El servidor cumple simultáneamente el rol de
ControlNode (metadatos) y de DataNode (bloques); en el hito 2 esos roles se separan en procesos
distintos sin cambiar el contrato del cliente.

## Estructura

```
dfsha/
  paths.py            normalización de rutas (compartida cliente/servidor)
  server/
    config.py         parámetros por variables de entorno
    auth.py           hash de contraseñas (PBKDF2) y tokens JWT
    metadata.py       esquema y consultas SQLite (ControlNode)
    storage.py        lectura/escritura de bloques en disco (DataNode)
    main.py           API REST (FastAPI)
  client/
    api.py            cliente HTTP de la API
    cli.py            shell interactivo
```

## Ejecución

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

uvicorn dfsha.server.main:app --host 0.0.0.0 --port 8000
```

Cliente, en otra terminal:

```bash
python -m dfsha.client.cli --server http://localhost:8000 --user nico --register
python -m dfsha.client.cli --server http://localhost:8000 --user nico
```

Con Docker:

```bash
docker build -t dfsha .
docker run -p 8000:8000 -v dfsha-data:/data dfsha
```

La documentación interactiva de la API queda en `http://localhost:8000/docs`.

### Variables de entorno

| Variable | Valor por defecto | Descripción |
| --- | --- | --- |
| `DFSHA_HOME` | `./data` | Carpeta con la base de metadatos y los bloques |
| `DFSHA_BLOCK_SIZE` | `4194304` (4 MiB) | Tamaño de bloque en bytes |
| `DFSHA_SECRET` | `dfsha-dev-secret` | Llave para firmar los tokens JWT |
| `DFSHA_TOKEN_TTL` | `3600` | Vigencia del token en segundos |

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

## API REST

Todas las rutas, salvo las de autenticación, requieren el encabezado
`Authorization: Bearer <token>`.

| Método | Ruta | Descripción |
| --- | --- | --- |
| POST | `/auth/register` | Crea un usuario y su directorio raíz |
| POST | `/auth/login` | Devuelve un token JWT |
| GET | `/fs/ls?path=` | Lista directorios y archivos |
| GET | `/fs/stat?path=` | Tipo, tamaño y fecha de una ruta |
| POST | `/fs/mkdir` | Crea un directorio |
| POST | `/fs/rmdir` | Borra un directorio vacío |
| DELETE | `/fs/rm?path=` | Borra un archivo |
| POST | `/files/upload?path=` | Sube el archivo en el cuerpo de la petición |
| GET | `/files/download?path=` | Devuelve el archivo reensamblado |
| GET | `/files/blocks?path=` | Devuelve el plan de bloques del archivo |
| GET | `/blocks/{id}` | Devuelve el contenido de un bloque |

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

- `users`: usuario, hash PBKDF2-SHA256 y salt.
- `directories`: árbol por usuario (`parent_id` nulo en la raíz de cada usuario).
- `files`: nombre, tamaño y fecha, únicos dentro de un directorio.
- `blocks`: identificador, archivo, secuencia y tamaño de cada bloque.

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

- Contraseñas con PBKDF2-SHA256 (200.000 iteraciones) y salt por usuario.
- Tokens JWT firmados con HS256 y vigencia configurable.
- Aislamiento por usuario: todas las consultas de metadatos filtran por `user_id`, incluida la
  lectura de bloques sueltos.

## Pendiente

- Hito 2: separar ControlNode y DataNodes, registro y heartbeat de nodos, ubicación de bloques.
- Hito 3: replicación de bloques, consistencia, alta disponibilidad del ControlNode, TLS entre
  nodos y cifrado de los bloques en disco.

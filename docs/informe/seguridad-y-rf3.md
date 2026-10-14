# Informe — Seguridad (RNF6) y acceso a archivos (RF3)

Sección del informe técnico a cargo de Nicolás. Se pega en el informe final.

## 1. Modelo de amenazas

| Amenaza | Control |
| --- | --- |
| Alguien escucha el tráfico entre cliente y nodos, o entre nodos | TLS 1.2+ en todos los nodos con CA propia del clúster |
| Robo del disco o acceso al volumen de un DataNode | Bloques cifrados con AES-256-GCM |
| Un bloque se corrompe o alguien lo modifica en disco | Tag GCM (lo detecta el nodo) + SHA-256 por bloque y por archivo (lo detecta el cliente) |
| Robo o adivinación de contraseñas | PBKDF2-SHA256 con 200.000 iteraciones y salt, longitud mínima, bloqueo tras fallos seguidos, 2FA TOTP |
| Un usuario intenta leer archivos de otro | ACL por usuario/grupo verificada en el ControlNode; respuesta 404 sin permiso |
| Un cliente pide directamente bloques a un DataNode | El DataNode exige un ticket firmado por el ControlNode para ese bloque y operación |
| Un proceso ajeno se hace pasar por nodo del clúster | Tokens de nodo firmados con el secreto del clúster; opcionalmente mTLS |
| Fuga de secretos por el repositorio | Secretos por variables de entorno o archivos generados con permisos 600; `certs/` y `*.key` ignorados por git |

## 2. Cifrado en tránsito

Cada nodo arranca con `python -m dfsha.server.run`, que activa https cuando recibe
`DFSHA_TLS_CERT` y `DFSHA_TLS_KEY`. `scripts/gen_certs.py` crea una CA privada (ECDSA P-256)
y un certificado por nodo con sus IPs y nombres DNS como SAN. Los clientes solo necesitan
`ca.crt` para validar al servidor, de modo que un atacante sin la llave de la CA no puede
suplantar un nodo. Con `DFSHA_TLS_CA` y `DFSHA_TLS_REQUIRE_CLIENT_CERT=1` un nodo interno exige
además certificado de cliente (mTLS), útil para los puertos de replicación entre DataNodes.

## 3. Cifrado en reposo

Formato de un bloque en disco:

```
"DFS1" (4 B) | nonce aleatorio (12 B) | AES-256-GCM(bloque) + tag (16 B)
```

El identificador del bloque se pasa como dato asociado (AAD). Así, además de la
confidencialidad, GCM garantiza que el bloque no fue alterado ni intercambiado por otro: si
alguien copia el bloque A sobre el archivo del bloque B, el descifrado de B falla. La
sobrecarga es de 32 bytes por bloque (0,0008 % con bloques de 4 MiB). La llave se pasa por
`DFSHA_STORAGE_KEY` o se genera en el primer arranque.

## 4. Integridad extremo a extremo

Durante la subida, el ControlNode calcula el SHA-256 de cada bloque y del archivo completo y
los guarda en los metadatos. El plan de lectura (`/files/blocks`) incluye esos checksums y el
cliente los verifica al recibir cada bloque y al terminar el archivo (`get` muestra
"SHA-256 verificado"). Esto protege también contra errores en una réplica (hito 3): si una
réplica devuelve datos distintos, el cliente lo detecta y puede pedir el bloque a otra.

## 5. Autenticación

- **Usuario/contraseña:** PBKDF2-SHA256, 200.000 iteraciones, salt de 128 bits por usuario,
  comparación en tiempo constante, mínimo 8 caracteres.
- **Sesión:** JWT HS256 con vencimiento (`DFSHA_TOKEN_TTL`).
- **2FA:** TOTP (RFC 6238, 6 dígitos, 30 s, ventana ±1). `2fa setup` muestra un QR en la
  terminal para Google/Microsoft Authenticator; con el 2FA activo el login responde 401 con
  `X-DFSha-OTP: required` hasta recibir el código.
- **API keys:** para scripts y automatización. Formato `dfsha_<prefijo>_<secreto>`, se muestra
  una sola vez y en la base queda solo su SHA-256; se pueden listar (con último uso) y revocar.
- **Fuerza bruta:** después de 5 fallos seguidos el login de ese usuario responde 429 durante
  60 s (configurable).

## 6. Autorización: ACL por usuario y grupo

Cada usuario es dueño de su árbol. El dueño puede dar `r` (listar, leer, descargar) o `rw`
(además crear, reemplazar y borrar) a un usuario o a un grupo, sobre un archivo o sobre un
directorio; el permiso de un directorio se hereda a todo su contenido. El permiso efectivo es
el mayor entre el directo y el de los grupos del usuario. Las rutas ajenas se escriben
`/~dueño/ruta`. Si el usuario no tiene ningún permiso la respuesta es 404, para no revelar qué
rutas existen; si tiene `r` e intenta escribir, 403.

Ejemplo:

```
nico> group new equipo
nico> group add equipo abraham
nico> share /proyecto @equipo rw
abraham> ls /~nico/proyecto
abraham> put informe.pdf /~nico/proyecto/informe.pdf
```

## 7. Seguridad entre nodos de control y de datos

Inspirado en los *block access tokens* de HDFS (`dfsha/cluster_auth.py`):

1. El cliente pide el plan de un archivo al ControlNode con su token de usuario.
2. El ControlNode verifica la ACL y devuelve, por cada bloque, un ticket JWT firmado con
   `DFSHA_CLUSTER_SECRET` que contiene el id del bloque, la operación (`r`/`w`) y vence en
   5 minutos.
3. El cliente presenta el ticket al DataNode (`X-Block-Ticket`). El DataNode verifica firma,
   bloque, operación y vencimiento **sin consultar la base de metadatos**, así que escala de
   forma independiente del ControlNode.

Un ticket de un bloque no sirve para otro bloque, un ticket de lectura no sirve para
escribir y un ticket vencido o firmado con otro secreto se rechaza (todo cubierto por pruebas).
Para registro, heartbeat y replicación los nodos usan tokens de nodo firmados con el mismo
secreto.

## 8. RF3 — acceso a archivos

Además de la transferencia completa (`put`/`get`), el cliente ofrece un descriptor de archivo
(`dfsha/client/files.py`) con `open`, `read`, `seek`, `tell`, `write`, `close`, `lock` y
`unlock`.

- **Lectura aleatoria:** `read(n)` calcula qué bloques cubren `[posición, posición + n)` y pide
  solo esos rangos (`/blocks/{id}?offset=&length=`). Leer 64 bytes de un archivo de 1 GB
  transfiere 64 bytes.
- **Escritura:** DFSha sigue el modelo de object storage (WORM): no se modifica un bloque en
  sitio. `write` acumula en un temporal local y `close` sube la versión nueva, que reemplaza a
  la anterior en una sola transacción del ControlNode (`overwrite=true`); los lectores ven la
  versión vieja o la nueva, nunca una mezcla. Los bloques viejos se borran después del cambio.
- **Locks:** abrir en `w`/`a` toma un lock exclusivo con *lease* (60 s por defecto) que el
  cliente renueva en segundo plano. Mientras exista, otro usuario recibe 423 al escribir o
  borrar. Si el cliente se cae, el lease vence solo y el archivo no queda bloqueado para
  siempre.

## 9. Pruebas

`pytest -q` levanta un servidor real con TLS y ejecuta 24 pruebas:

| Prueba | Resultado esperado |
| --- | --- |
| Cliente sin la CA / http plano contra el puerto TLS | Conexión rechazada |
| Bloque en disco | No contiene el texto plano |
| Bloque alterado o intercambiado | `CorruptBlock` |
| Vectores TOTP del RFC 6238 | Códigos correctos |
| Ticket de otro bloque, de lectura usado para escribir, vencido o con otro secreto | Rechazado |
| Contraseña corta | 400 |
| 3 fallos seguidos de login | 429 y desbloqueo al vencer |
| Login con 2FA activo sin código / con código | 401 `X-DFSha-OTP` / token |
| API key válida / revocada | Acceso / 401 |
| ACL `r`: listar y leer sí; subir, borrar o re-compartir no; ruta privada | 200 / 403 / 404 |
| ACL `rw` por grupo, retiro del miembro | Escribe / pierde acceso |
| Bloque de otro usuario pedido por id | 404 |
| `seek` + `read` en medio y al final del archivo | Bytes exactos |
| `write`, `append` y reemplazo con checksum actualizado | Contenido y SHA-256 correctos |
| Lock de otro usuario al reemplazar o borrar | 423; se libera al cerrar o al vencer |

# Informe — Entorno de ejecución en AWS

Sección del informe técnico a cargo de Nicolás ("Descripción del entorno de ejecución").

## 1. Infraestructura

El clúster corre en AWS Academy Learner Lab, región us-east-1, sobre la VPC por defecto.

| Nodo | Rol | Zona | Tipo | Dirección |
| --- | --- | --- | --- | --- |
| cn1 | ControlNode primario | us-east-1a | t3.small | Elastic IP |
| cn2 | ControlNode de respaldo | us-east-1b | t3.small | Elastic IP |
| dn1 | DataNode | us-east-1a | t3.small | Elastic IP |
| dn2 | DataNode | us-east-1b | t3.small | Elastic IP |
| dn3 | DataNode | us-east-1c | t3.small | Elastic IP |

- **Distribución en tres zonas de disponibilidad:** la caída de una zona deja vivo al menos
  un ControlNode y dos DataNodes.
- **Elastic IPs:** el Learner Lab apaga las máquinas al cerrar la sesión. Sin Elastic IP la
  dirección pública cambia en cada arranque y habría que regenerar certificados y la
  configuración de los DataNodes.
- **Cada nodo ejecuta un contenedor Docker** con la misma imagen (`Dockerfile` del
  repositorio). El rol lo decide la variable `DFSHA_APP`: `dfsha.server.main:app` para el
  ControlNode y `dfsha.server.datanode:app` para el DataNode.
- **Comunicación sobre Internet:** todos los nodos se anuncian con su Elastic IP. El cliente
  habla con el ControlNode y descarga los bloques directamente de los DataNodes.

## 2. Despliegue reproducible

El despliegue completo está en `deploy/aws/` y se ejecuta desde AWS CloudShell:

1. `01-infra.sh` crea el Security Group, las 5 instancias (Amazon Linux 2023 con Docker,
   IMDSv2 obligatorio, `LabInstanceProfile`) y asocia una Elastic IP a cada una.
2. `02-certs.sh` genera los secretos compartidos y una CA propia con un certificado por
   nodo, cuyo SAN incluye su Elastic IP, IP privada y nombre DNS.
3. `03-deploy.sh <rama>` prepara para cada nodo un paquete con sus certificados, secretos
   y variables de rol. Lo entrega por una URL firmada de S3 de 10 minutos y, mediante AWS
   Systems Manager Run Command, clona la rama, construye la imagen y arranca el contenedor.
   No se usa SSH en ningún momento.
4. `status.sh` verifica cada nodo por HTTPS validando el certificado con la CA del clúster.

Actualizar el código del clúster es volver a ejecutar `03-deploy.sh` con la rama deseada.

## 3. Seguridad de la infraestructura

| Control | Implementación |
| --- | --- |
| Superficie de red | Security Group con un único puerto de entrada, 8000/TCP (HTTPS). Sin puerto 22. |
| Administración | SSM Run Command con el rol del laboratorio; no hay llaves SSH ni contraseñas en las máquinas. |
| Metadatos de instancia | IMDSv2 obligatorio (`HttpTokens=required`). |
| Cifrado en tránsito | TLS en los 5 nodos con CA propia; DataNode ↔ ControlNode validan certificados. |
| Cifrado en reposo | Cada DataNode cifra sus bloques con AES-256-GCM y una llave propia que no sale del nodo. |
| Secretos | Generados en CloudShell, fuera de git; viajan por S3 privado con URL firmada y se borran al terminar. |

## 4. Costos

5 × t3.small (USD 0,0208/h) + 5 IPv4 públicas (USD 0,005/h) ≈ **USD 0,13 por hora encendido**.
Los volúmenes gp3 de 16 GiB suman ≈ USD 6,40 al mes aunque las máquinas estén apagadas. Al
terminar el proyecto, `99-cleanup.sh --confirmar` borra instancias, Elastic IPs, Security
Group y bucket.

## 5. Resultados de las pruebas en AWS (14-oct-2026)

Clúster: cn1 `52.7.88.60`, cn2 `44.221.198.40`, dn1 `100.49.125.212`, dn2 `52.73.218.148`,
dn3 `50.19.194.118`. Las pruebas de cliente se hicieron desde AWS CloudShell, fuera de la
VPC de los nodos, hablando con ellos por Internet con TLS.

| Prueba | Resultado |
| --- | --- |
| Subida de 50 MB | 13 bloques de 4 MiB repartidos en orden dn1 → dn2 → dn3 (5/4/4) |
| Descarga | SHA-256 verificado por bloque y por archivo; `sha256sum` idéntico al original |
| HTTP plano / HTTPS sin la CA / DataNode sin ticket | Conexión rechazada / certificado no confiable / 401 |
| Bloque en disco | Marca `DFS1` + texto cifrado distinto de los bytes originales |
| Caída de dn2 | El ControlNode deja de asignarle bloques a los 30 s; la subida siguiente va a dn1 y dn3; dn2 vuelve y se re-registra solo |
| Replicación factor 3 | Al activarla, la re-replicación hizo 52 copias DataNode → DataNode (26 bloques × 2); cada DataNode quedó con los 26 bloques |
| Lectura con 2 de 3 DataNodes caídos | Archivo de 50 MB descargado completo desde dn3, SHA-256 idéntico |
| Caída del ControlNode primario | cn2 se promueve a `primary/1` en ~10 s; el cliente (lista cn1,cn2) sigue: `ls`, `get` verificado y `put` |
| Regreso del primario viejo | cn1 vuelve como `standby/1` (fencing por época): nunca hay dos primarios |
| Benchmark (CloudShell, misma región) | 32 MB: 0,242 s secuencial vs 0,241 s paralelo (1,01x); 128 MB: 0,945 s vs 0,811 s (1,17x) |

Sobre el benchmark: dentro de la misma región la red da ~140 MB/s y el cuello de botella
es el cliente (descifrar TLS y verificar SHA-256 en un solo equipo), por eso el paralelismo
apenas mejora. La ganancia del paralelismo crece con la latencia entre cliente y DataNodes
(por ejemplo, un cliente en Colombia contra us-east-1), porque varias descargas
simultáneas ocultan el tiempo de ida y vuelta de cada bloque.

## 6. Evidencias

| Código | Evidencia |
| --- | --- |
| E01 | Learner Lab activo y CloudShell en us-east-1 |
| E02 | Salida de `01-infra.sh` con las 5 Elastic IPs |
| E03 | Instancias en 3 zonas; Details con LabRole, IMDSv2 Required y sin key pair |
| E04 | Security Group con una sola regla de entrada (8000/TCP) |
| E05 | Elastic IPs asociadas |
| E06 | Certificados de cada nodo con su Elastic IP en el SAN |
| E07 | `03-deploy.sh` con los 5 nodos OK por HTTPS |
| E08 | `put` de 50 MB: 13 bloques repartidos entre dn1, dn2 y dn3 |
| E09 | `get` con SHA-256 verificado y hash idéntico |
| E10 | Bloques guardados en el disco de cada DataNode |
| E11 | HTTPS con CA: 200; HTTP plano y HTTPS sin CA: rechazados; DataNode sin ticket: 401 |
| E12 | Bloque en disco cifrado frente a los bytes originales |
| E13–E15 | Caída de dn2, nuevos bloques solo en dn1 y dn3, recuperación de dn2 |
| E16 | Benchmark secuencial vs. paralelo (32 y 128 MB) |
| E17 | Failover: cn1 caído, cn2 `primary/1`, el cliente sigue operando |
| E18 | cn1 vuelve como `standby/1` |
| E20 | ControlNodes `primary/0` y `standby/0` tras activar la HA |
| E21 | Re-replicación: 52 copias DataNode → DataNode, 26 bloques en cada DataNode |
| E22 | Lectura completa con dn1 y dn2 caídos |
| E19 | Cleanup: sin instancias ni Elastic IPs de dfsha (después de sustentar) |

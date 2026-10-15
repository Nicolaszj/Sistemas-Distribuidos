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

## 5. Evidencias

| Código | Evidencia |
| --- | --- |
| E02–E05 | Salida de `01-infra.sh`, instancias en 3 zonas, Security Group y Elastic IPs |
| E06 | Certificados con las Elastic IP en el SAN |
| E07 | `status.sh` con los 5 nodos OK por HTTPS |
| E08–E09 | `put` de 50 MB repartido entre dn1, dn2 y dn3; `get` con SHA-256 verificado |
| E10–E11 | HTTP plano rechazado, DataNode sin ticket rechazado, bloque cifrado en disco |
| E12–E13 | Caída de dn2: el ControlNode deja de asignarle bloques; vuelve tras reiniciarlo |
| E14 | Benchmark secuencial vs. paralelo desde el PC contra AWS |

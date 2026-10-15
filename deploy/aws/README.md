# Despliegue de DFSha en AWS

Despliegue reproducible del clúster en AWS Academy Learner Lab (región us-east-1).
Todo se ejecuta desde **AWS CloudShell**, dentro de este repositorio. No usa SSH: los
nodos se administran con AWS Systems Manager (SSM) a través de `LabInstanceProfile`.

| Nodo | Rol | Zona |
| --- | --- | --- |
| cn1 | ControlNode primario | us-east-1a |
| cn2 | ControlNode de respaldo (HA) | us-east-1b |
| dn1 | DataNode | us-east-1a |
| dn2 | DataNode | us-east-1b |
| dn3 | DataNode | us-east-1c |

Cada nodo es una EC2 `t3.small` con Amazon Linux 2023, una Elastic IP y un contenedor
Docker `dfsha` que expone la API por HTTPS en el puerto 8000.

## Pasos

```bash
git clone https://github.com/Nicolaszj/Sistemas-Distribuidos.git
cd Sistemas-Distribuidos
bash deploy/aws/01-infra.sh          # SG, 5 EC2, Elastic IPs (≈5 min)
bash deploy/aws/02-certs.sh          # secretos del clúster y certificados TLS
bash deploy/aws/03-deploy.sh main    # clona la rama, construye la imagen y arranca (≈6 min)
bash deploy/aws/status.sh            # enciende lo apagado y revisa cada nodo por HTTPS
```

El cliente solo necesita `deploy/aws/out/certs/ca.crt` (descargarlo desde CloudShell):

```bash
python -m dfsha.client.cli --server https://<EIP de cn1>:8000 --ca aws-ca.crt --user nico
```

## Operación

| Comando | Para qué |
| --- | --- |
| `bash deploy/aws/03-deploy.sh <rama> [nodo ...]` | Actualiza el código en todos los nodos o en algunos |
| `bash deploy/aws/node.sh <nodo> stop\|start\|restart` | Simula la caída y recuperación de un nodo |
| `bash deploy/aws/node.sh <nodo> logs\|ps\|peek` | Log, estado y bloques cifrados del nodo |
| `bash deploy/aws/status.sh` | Estado del clúster (y enciende nodos apagados) |
| `bash deploy/aws/99-cleanup.sh --confirmar` | Borra todo lo creado |

## Configuración por nodo

`deploy/aws/nodes/<nodo>.env` tiene las variables propias de cada rol. `03-deploy.sh`
reemplaza `{{NODE}}`, `{{SELF_EIP}}` y `{{EIP_<nodo>}}` por los valores reales y agrega:

- `DFSHA_TLS_CERT`, `DFSHA_TLS_KEY`, `DFSHA_TLS_CA`: certificado del nodo y CA del clúster.
- `DFSHA_SECRET`, `DFSHA_CLUSTER_SECRET`: iguales en todos los nodos (`out/secrets.env`).
- `DFSHA_STORAGE_KEY`: solo en los dos ControlNodes y la misma en ambos
  (`out/cn-secrets.env`), porque el respaldo reemplaza al primario.

Alta disponibilidad: cn1 arranca como primario y cn2 como respaldo (`DFSHA_HA_ROLE`,
`DFSHA_HA_PEER_URL`); los DataNodes conocen a los dos (`DFSHA_CONTROL_URLS`) y el
cliente también: `--server https://<cn1>:8000,https://<cn2>:8000`. La columna HA de
`status.sh` muestra el rol y la época de cada ControlNode.

La llave AES de los bloques de los DataNodes no se reparte: cada DataNode genera la suya
en su volumen y nunca sale de la máquina. Cuando un bloque se re-replica, el DataNode
origen lo descifra con su llave y el destino lo vuelve a cifrar con la propia.

## Seguridad del despliegue

- Security Group con un único puerto de entrada (8000/TCP). Sin SSH ni puerto 22.
- TLS en todos los nodos con una CA propia; los DataNodes validan al ControlNode y
  viceversa con esa CA.
- IMDSv2 obligatorio en las instancias.
- Secretos y llaves solo en `deploy/aws/out/` de CloudShell (ignorado por git). Viajan a
  cada nodo por una URL firmada de S3 de 10 minutos, en un bucket privado que se vacía al
  terminar cada despliegue.

## Learner Lab

- Al cerrar la sesión el laboratorio apaga las instancias. Las Elastic IP mantienen las
  direcciones y los contenedores arrancan solos (`--restart unless-stopped`); basta con
  `bash deploy/aws/status.sh` al volver.
- `deploy/aws/out/` vive en el disco de CloudShell. Si se pierde, `01-infra.sh` reconstruye
  `cluster.env` sin crear nada nuevo, pero `02-certs.sh` crea una CA nueva: hay que volver a
  desplegar y descargar otra vez `ca.crt`.

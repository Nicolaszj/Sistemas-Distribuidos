import uuid

from fastapi.testclient import TestClient

from dfsha import cluster_auth
from dfsha.server import config, datanode, main


def test_datanode_validates_block_ticket():
    block_id = uuid.uuid4().hex
    ticket = cluster_auth.issue_block_ticket(
        config.CLUSTER_SECRET, block_id, cluster_auth.WRITE
    )
    with TestClient(datanode.app) as client:
        response = client.put(
            f"/blocks/{block_id}",
            content=b"distributed",
            headers={"X-Block-Ticket": ticket},
        )
        assert response.status_code == 200
        read_ticket = cluster_auth.issue_block_ticket(
            config.CLUSTER_SECRET, block_id, cluster_auth.READ
        )
        response = client.get(
            f"/blocks/{block_id}", headers={"X-Block-Ticket": read_ticket}
        )
        assert response.content == b"distributed"
        response = client.get(
            f"/blocks/{uuid.uuid4().hex}",
            headers={"X-Block-Ticket": read_ticket},
        )
        assert response.status_code == 403


def test_controlnode_registers_and_heartbeats_node():
    node_id = f"test-{uuid.uuid4().hex}"
    token = cluster_auth.issue_node_token(config.CLUSTER_SECRET, node_id)
    with TestClient(main.app) as client:
        response = client.post(
            "/nodes/register",
            json={"node_id": node_id, "url": "http://datanode.test:8000"},
            headers={"X-Node-Token": token},
        )
        assert response.status_code == 200
        response = client.post(
            f"/nodes/{node_id}/heartbeat",
            headers={"X-Node-Token": token},
        )
        assert response.status_code == 200

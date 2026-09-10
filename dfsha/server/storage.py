import uuid

from . import config


def path_for(block_id):
    return config.BLOCKS_DIR / block_id[:2] / block_id


def write(data):
    block_id = uuid.uuid4().hex
    destination = path_for(block_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return block_id


def read(block_id):
    return path_for(block_id).read_bytes()


def remove(block_id):
    path_for(block_id).unlink(missing_ok=True)

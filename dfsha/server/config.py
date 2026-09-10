import os
from pathlib import Path

HOME = Path(os.getenv("DFSHA_HOME", Path(__file__).resolve().parents[2] / "data"))
DATABASE_PATH = HOME / "metadata.db"
BLOCKS_DIR = HOME / "blocks"

BLOCK_SIZE = int(os.getenv("DFSHA_BLOCK_SIZE", 4 * 1024 * 1024))
SECRET_KEY = os.getenv("DFSHA_SECRET", "dfsha-dev-secret")
TOKEN_TTL_SECONDS = int(os.getenv("DFSHA_TOKEN_TTL", 3600))

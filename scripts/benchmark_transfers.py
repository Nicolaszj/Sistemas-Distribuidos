"""Compara lectura secuencial y paralela de bloques desde un DFSha activo.

Uso:
    python scripts/benchmark_transfers.py --server http://localhost:8000 \
        --user demo --password contrasena --size-mb 32
    python scripts/benchmark_transfers.py --server https://cn1:8000,https://cn2:8000 \
        --ca ca.crt --api-key "$(cat ~/.dfsha-key)" --size-mb 32
"""

import argparse
import hashlib
import io
import time

import sys
from pathlib import Path

# Permite ejecutarlo como `python scripts/benchmark_transfers.py` desde la raíz.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dfsha.client.api import DfsClient  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--user")
    parser.add_argument("--password")
    parser.add_argument("--api-key", help="alternativa a --user/--password")
    parser.add_argument("--ca")
    parser.add_argument("--size-mb", type=int, default=32)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    servers = [url.strip() for url in args.server.split(",") if url.strip()]
    client = DfsClient(servers, ca_file=args.ca, api_key=args.api_key)
    if not args.api_key:
        if not (args.user and args.password):
            parser.error("usa --api-key o --user y --password")
        client.login(args.user, args.password)
    payload = (b"dfsha-benchmark-" * (args.size_mb * 1024 * 1024 // 16 + 1))[
        : args.size_mb * 1024 * 1024
    ]
    path = f"/benchmark-{time.time_ns()}.bin"
    client.upload(path, io.BytesIO(payload))
    plan = client.blocks(path)

    start = time.perf_counter()
    sequential = [client.read_block(block) for block in plan["blocks"]]
    sequential_time = time.perf_counter() - start

    start = time.perf_counter()
    parallel = client.read_blocks_parallel(plan["blocks"], args.workers)
    parallel_time = time.perf_counter() - start

    assert b"".join(sequential) == payload
    assert b"".join(parallel) == payload
    assert hashlib.sha256(payload).hexdigest() == plan["checksum"]
    print(f"bloques={len(plan['blocks'])} bytes={len(payload)}")
    print(f"secuencial={sequential_time:.3f}s")
    print(f"paralelo={parallel_time:.3f}s workers={args.workers}")
    if parallel_time:
        print(f"aceleracion={sequential_time / parallel_time:.2f}x")


if __name__ == "__main__":
    main()

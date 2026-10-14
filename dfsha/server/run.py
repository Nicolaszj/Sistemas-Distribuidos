"""Arranca un nodo de DFSha, con TLS si se configuran certificado y llave.

    DFSHA_TLS_CERT=certs/controlnode.crt DFSHA_TLS_KEY=certs/controlnode.key \
        python -m dfsha.server.run

Variables:
    DFSHA_APP       aplicación ASGI a servir (por defecto dfsha.server.main:app);
                    sirve para lanzar otros roles (p. ej. el DataNode) con el mismo TLS
    DFSHA_HOST      interfaz (0.0.0.0)
    DFSHA_PORT      puerto (8000)
    DFSHA_TLS_CERT  certificado del nodo (PEM) — activa https
    DFSHA_TLS_KEY   llave privada del nodo (PEM)
    DFSHA_TLS_CA    CA del clúster; con DFSHA_TLS_REQUIRE_CLIENT_CERT=1 el nodo exige
                    certificado de cliente firmado por ella (mTLS entre nodos internos)
"""

import os
import ssl

import uvicorn


def main():
    options = {
        "host": os.getenv("DFSHA_HOST", "0.0.0.0"),
        "port": int(os.getenv("DFSHA_PORT", 8000)),
    }

    cert, key = os.getenv("DFSHA_TLS_CERT"), os.getenv("DFSHA_TLS_KEY")
    if cert and key:
        options.update(ssl_certfile=cert, ssl_keyfile=key)
        ca = os.getenv("DFSHA_TLS_CA")
        if ca:
            options["ssl_ca_certs"] = ca
            if os.getenv("DFSHA_TLS_REQUIRE_CLIENT_CERT") == "1":
                options["ssl_cert_reqs"] = ssl.CERT_REQUIRED
        print(f"DFSha con TLS en https://{options['host']}:{options['port']}")
    else:
        print("ADVERTENCIA: DFSha sin TLS (define DFSHA_TLS_CERT y DFSHA_TLS_KEY)")

    uvicorn.run(os.getenv("DFSHA_APP", "dfsha.server.main:app"), **options)


if __name__ == "__main__":
    main()

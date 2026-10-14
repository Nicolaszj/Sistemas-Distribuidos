"""Genera la CA privada del clúster y certificados para cada nodo.

Funciona igual en Windows, Linux y macOS (no necesita openssl instalado).

    python scripts/gen_certs.py controlnode --san localhost 127.0.0.1 54.12.34.56
    python scripts/gen_certs.py datanode1 --san 10.0.1.11 ec2-....compute.amazonaws.com

La primera ejecución crea certs/ca.crt y certs/ca.key; las siguientes reutilizan
la misma CA. Los clientes solo necesitan certs/ca.crt (opción --ca del CLI).
La llave de la CA y las de los nodos NUNCA se suben al repositorio.
"""

import argparse
import datetime
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def save_key(key, path):
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass


def load_or_create_ca(directory):
    cert_path, key_path = directory / "ca.crt", directory / "ca.key"
    if cert_path.exists() and key_path.exists():
        key = serialization.load_pem_private_key(key_path.read_bytes(), None)
        return x509.load_pem_x509_certificate(cert_path.read_bytes()), key

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "DFSha CA")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True,
                content_commitment=False, key_encipherment=False, data_encipherment=False,
                key_agreement=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    save_key(key, key_path)
    print(f"CA creada en {cert_path}")
    return cert, key


def subject_alternative_names(values):
    names = []
    for value in values:
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(value)))
        except ValueError:
            names.append(x509.DNSName(value))
    return names


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("name", help="nombre del nodo, p. ej. controlnode o datanode1")
    parser.add_argument("--san", nargs="*", default=[], help="IPs y nombres DNS del nodo")
    parser.add_argument("--out", default="certs", help="carpeta de salida (certs)")
    parser.add_argument("--days", type=int, default=825)
    arguments = parser.parse_args()

    directory = Path(arguments.out)
    directory.mkdir(parents=True, exist_ok=True)
    ca_cert, ca_key = load_or_create_ca(directory)

    sans = ["localhost", "127.0.0.1", arguments.name, *arguments.san]
    sans = list(dict.fromkeys(sans))
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, arguments.name)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=arguments.days))
        .add_extension(x509.SubjectAlternativeName(subject_alternative_names(sans)), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    cert_path = directory / f"{arguments.name}.crt"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    save_key(key, directory / f"{arguments.name}.key")
    print(f"Certificado de {arguments.name} en {cert_path} (SAN: {', '.join(sans)})")


if __name__ == "__main__":
    main()

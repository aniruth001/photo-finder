"""Network helpers shared by the local app and the studio: LAN address and a self-signed certificate.

Kept apart from app.py so importing them doesn't load dlib.
"""
import socket
from pathlib import Path


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent; just picks the LAN interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def all_ips() -> list:
    """Every IPv4 address of this computer (useful with USB tethering, where the LAN address differs)."""
    ips = {lan_ip()}
    try:
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    return sorted(i for i in ips if not i.startswith("127."))


def ensure_cert(cache_root: Path, ip: str):
    """Self-signed certificate for the LAN address, so the phone can use https."""
    crt, key = cache_root / f"lan-{ip}.crt", cache_root / f"lan-{ip}.key"
    if crt.exists() and key.exists():
        return crt, key
    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "photo-finder")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(k.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName(
            [x509.IPAddress(ipaddress.ip_address(ip)), x509.DNSName("localhost")]), critical=False)
        .sign(k, hashes.SHA256())
    )
    cache_root.mkdir(parents=True, exist_ok=True)
    key.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                    serialization.NoEncryption()))
    crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return crt, key

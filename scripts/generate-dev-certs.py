#!/usr/bin/env python3
"""Generate a self-signed TLS cert/key pair for local HTTPS development.

Why this exists
----------------
Production TLS is handled by Caddy (`infra/caddy/Caddyfile.template`,
automatic Let's Encrypt certs) or by whatever terminates TLS in front of the
Kubernetes ingress (`infra/k8s/`, `infra/helm/`) — this script is NOT for
those. It's for the local-only case: testing a service (or
`scripts/trade_dashboard.py`, or a tool built from `packaging/dashboard/`)
over `https://` on your own machine, where a CA-signed cert isn't available
and isn't needed.

Usage
-----
    python scripts/generate-dev-certs.py
    python scripts/generate-dev-certs.py --out certs --days 730
    python scripts/generate-dev-certs.py --cn dashboard.local --san 127.0.0.1 --san ::1

Requires the `cryptography` package (`pip install cryptography`) or, as a
fallback with no Python dependency, the `openssl` CLI on PATH (Git Bash on
Windows ships one).

Output: <out>/dev-cert.pem and <out>/dev-key.pem (default --out: ./certs).
Both are gitignored — see .gitignore. Load them the same way
scripts/trade_dashboard.py or any Flask/FastAPI dev server would, e.g.:

    app.run(ssl_context=("certs/dev-cert.pem", "certs/dev-key.pem"))
    uvicorn.run(app, ssl_certfile="certs/dev-cert.pem", ssl_keyfile="certs/dev-key.pem")

This is a *development* helper. The generated cert is self-signed and will
show a browser warning until you either add it to your local trust store or
just click through — do not point a real domain or production traffic at it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import sys
from pathlib import Path


def _generate_with_cryptography(
    out_dir: Path, common_name: str, sans: list[str], days: int
) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])

    san_entries: list[x509.GeneralName] = []
    for entry in sans:
        try:
            import ipaddress

            san_entries.append(x509.IPAddress(ipaddress.ip_address(entry)))
        except ValueError:
            san_entries.append(x509.DNSName(entry))

    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    key_path = out_dir / "dev-key.pem"
    cert_path = out_dir / "dev-cert.pem"
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    print(f"wrote {cert_path}")
    print(f"wrote {key_path}")


def _generate_with_openssl(
    out_dir: Path, common_name: str, sans: list[str], days: int
) -> None:
    key_path = out_dir / "dev-key.pem"
    cert_path = out_dir / "dev-cert.pem"
    san_str = ",".join(
        f"IP:{s}" if s.replace(".", "").isdigit() or ":" in s else f"DNS:{s}"
        for s in sans
    )
    cmd = [
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(key_path),
        "-out",
        str(cert_path),
        "-days",
        str(days),
        "-subj",
        f"/CN={common_name}",
    ]
    if san_str:
        cmd += ["-addext", f"subjectAltName={san_str}"]
    subprocess.run(cmd, check=True)
    print(f"wrote {cert_path}")
    print(f"wrote {key_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--out", default="certs", help="Output directory (default: ./certs)"
    )
    parser.add_argument(
        "--cn", default="localhost", help="Certificate common name (default: localhost)"
    )
    parser.add_argument(
        "--san",
        action="append",
        default=None,
        help="Subject alternative name; repeatable (default: localhost, 127.0.0.1, ::1)",
    )
    parser.add_argument(
        "--days", type=int, default=825, help="Validity period in days (default: 825)"
    )
    args = parser.parse_args(argv)

    sans = args.san or ["localhost", "127.0.0.1", "::1"]
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        _generate_with_cryptography(out_dir, args.cn, sans, args.days)
    except ImportError:
        print(
            "`cryptography` package not installed -- falling back to the "
            "openssl CLI. `pip install cryptography` to avoid needing openssl.",
            file=sys.stderr,
        )
        try:
            _generate_with_openssl(out_dir, args.cn, sans, args.days)
        except FileNotFoundError:
            print(
                "Neither the `cryptography` package nor an `openssl` binary "
                "on PATH is available. Install one of the two:\n"
                "  pip install cryptography\n"
                "or (Windows) use the openssl that ships with Git Bash.",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

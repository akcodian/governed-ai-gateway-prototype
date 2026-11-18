"""Ed25519 key management. Keys are generated at setup into a git-ignored folder."""
from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

REPO_ROOT = Path(__file__).resolve().parents[1]

# control_plane: signs snapshot.json
# approving_function: signs pairing entries
# gateway_tokens: gateway-issued JWTs (root budget, downstream scoped tokens)
# idp: simulated workload identity provider (workload JWTs)
KEY_NAMES = ("control_plane", "approving_function", "gateway_tokens", "idp")
SECRET_NAMES = ("admin_token", "internal_token")


def key_dir() -> Path:
    return Path(os.environ.get("GOV_KEY_DIR", REPO_ROOT / "keys"))


def generate_all(force: bool = False) -> list[str]:
    d = key_dir()
    d.mkdir(parents=True, exist_ok=True)
    created = []
    for name in KEY_NAMES:
        priv_path = d / f"{name}.key"
        if priv_path.exists() and not force:
            continue
        key = Ed25519PrivateKey.generate()
        priv_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
        (d / f"{name}.pub").write_bytes(
            key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        )
        created.append(name)
    for name in SECRET_NAMES:
        p = d / name
        if p.exists() and not force:
            continue
        p.write_text(secrets.token_urlsafe(32))
        created.append(name)
    return created


def load_private(name: str) -> Ed25519PrivateKey:
    return serialization.load_pem_private_key((key_dir() / f"{name}.key").read_bytes(), password=None)  # type: ignore[return-value]


def load_public(name: str) -> Ed25519PublicKey:
    return serialization.load_pem_public_key((key_dir() / f"{name}.pub").read_bytes())  # type: ignore[return-value]


def load_private_pem(name: str) -> bytes:
    return (key_dir() / f"{name}.key").read_bytes()


def load_public_pem(name: str) -> bytes:
    return (key_dir() / f"{name}.pub").read_bytes()


def read_secret(name: str) -> str:
    return (key_dir() / name).read_text().strip()


def sign_b64(name: str, data: bytes) -> str:
    return base64.b64encode(load_private(name).sign(data)).decode()


def verify_b64(public_key: Ed25519PublicKey, data: bytes, signature_b64: str | None) -> bool:
    if not signature_b64:
        return False
    try:
        public_key.verify(base64.b64decode(signature_b64), data)
        return True
    except (InvalidSignature, ValueError):
        return False


if __name__ == "__main__":
    made = generate_all()
    print(f"keys in {key_dir()}: created {made or 'nothing (already present)'}")

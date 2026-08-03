"""
Shared HTTPS helpers.

Python's bundled trust store can be missing or stale on some Windows builds
(e.g. PyInstaller/MSYS2), causing ``SSL: CERTIFICATE_VERIFY_FAILED`` on every
HTTPS request. These helpers build an SSL context that prefers certifi's
bundled CA bundle and falls back to the Windows system certificate store.
"""

from __future__ import annotations

import ssl
import sys

__all__ = ["make_ssl_context"]


def _windows_store_pems() -> list[str]:
    """Return PEM-encoded certificates from the Windows system cert store."""
    pems: list[str] = []
    for store_name in ("ROOT", "CA"):
        for cert_bytes, enc_type, _trust in ssl.enum_certificates(store_name) or []:
            if enc_type not in ("CERTIFICATE", "x509_asn"):
                continue
            try:
                pems.append(ssl.DER_cert_to_PEM_cert(cert_bytes))
            except Exception:
                continue
    return pems


def _windows_system_store_context() -> ssl.SSLContext | None:
    """Build an SSL context from the Windows system cert store, or None."""
    try:
        context = ssl.create_default_context()
        pems = _windows_store_pems()
        if not pems:
            return None
        context.load_verify_locations(cadata="\n".join(pems))
        return context
    except Exception:
        return None


def make_ssl_context() -> ssl.SSLContext:
    """Return an SSL context that can verify HTTPS against a known-good store."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass

    if sys.platform == "win32":
        context = _windows_system_store_context()
        if context is not None:
            return context

    return ssl.create_default_context()

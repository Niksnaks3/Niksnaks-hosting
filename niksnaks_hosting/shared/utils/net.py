from __future__ import annotations

import ssl
import sys

__all__ = ["make_ssl_context"]

def _windows_store_pems() -> list[str]:
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

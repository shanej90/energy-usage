"""
Authentication helpers for the Octopus Energy API.

Handles two concerns:
1. Building a requests.Session that works behind corporate/AV SSL inspection
   (Windows cert store is merged into the certifi CA bundle).
2. Obtaining a Kraken JWT token via the GraphQL API.
"""

import base64
import certifi
import configparser
import os
import platform
import ssl
import tempfile

import requests


def get_config(project_root: str = None) -> configparser.ConfigParser:
    """
    Read env.ini from the project root and return the ConfigParser object.
    If project_root is not given, it is discovered via directory_navigation.
    """
    if project_root is None:
        from utils.directory_navigation import find_project_root
        project_root = find_project_root()
    config = configparser.ConfigParser()
    config.read(os.path.join(project_root, "env.ini"))
    return config


def build_session() -> requests.Session:
    """
    Build a requests.Session with an augmented CA bundle.

    On Windows, the system certificate store is merged with certifi's bundle.
    This resolves TLS handshake failures caused by corporate/antivirus software
    that performs SSL inspection using its own CA.

    On other platforms the standard certifi bundle is used unchanged.
    """
    pem_certs = [open(certifi.where()).read()]

    if platform.system() == "Windows":
        for store in ("ROOT", "CA"):
            try:
                for cert_bytes, encoding, _trust in ssl.enum_certificates(store):
                    if encoding == "x509_asn":
                        b64 = base64.b64encode(cert_bytes).decode()
                        pem_certs.append(
                            "-----BEGIN CERTIFICATE-----\n"
                            + "\n".join(b64[i : i + 64] for i in range(0, len(b64), 64))
                            + "\n-----END CERTIFICATE-----"
                        )
            except Exception:
                pass  # non-fatal: fall back to certifi only

    ca_bundle = tempfile.NamedTemporaryFile(
        mode = "w", 
        suffix = ".pem",
        delete = False
        )
    ca_bundle.write("\n".join(pem_certs))
    ca_bundle.close()

    session = requests.Session()
    session.verify = ca_bundle.name
    return session


def get_token(session: requests.Session, api_key: str) -> str:
    """
    Obtain a short-lived Kraken JWT via the GraphQL API.

    The token is required for authenticated GraphQL queries (e.g. account info).
    The REST consumption/tariff endpoints use HTTP Basic Auth with the API key
    directly, so a JWT is only needed for GraphQL calls.
    """
    endpoint = "https://api.octopus.energy/v1/graphql/"
    mutation = """
    mutation obtainToken($apiKey: String!) {
      obtainKrakenToken(input: {APIKey: $apiKey}) {
        token
      }
    }
    """
    response = session.post(
        endpoint,
        json = {"query": mutation, "variables": {"apiKey": api_key}},
        headers = {"Content-Type": "application/json"},
        timeout = 30,
    )
    response.raise_for_status()
    return response.json()["data"]["obtainKrakenToken"]["token"]

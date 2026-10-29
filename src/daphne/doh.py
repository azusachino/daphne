"""Best-effort initial-host checks, not a sandbox for downloader traffic.

The subprocess resolves names independently and can follow redirects, so this
preflight does not prevent DNS rebinding or access to private destinations.
"""

import ipaddress
from urllib.parse import urlsplit

import httpx

DOH_URL = "https://cloudflare-dns.com/dns-query"


def _public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_global


def check_public_url(url: str) -> None:
    """Reject an initial URL if Cloudflare returns any non-public address.

    Both address families are queried. Resolver failures and empty results fail
    closed. Only the hostname is sent to Cloudflare, never the path or query.
    """
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Invalid downloader URL")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None:
            if not _public_address(host):
                raise ValueError("Downloader destination is not public")
            return
        name = host.encode("idna").decode("ascii")
        addresses = []
        with httpx.Client(
            timeout=5.0, trust_env=False, follow_redirects=False
        ) as client:
            for record_type in ("A", "AAAA"):
                response = client.get(
                    DOH_URL,
                    params={"name": name, "type": record_type},
                    headers={"Accept": "application/dns-json"},
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("Status") != 0 or not isinstance(
                    payload.get("Answer", []), list
                ):
                    raise ValueError("DNS lookup failed")
                for record in payload.get("Answer", []):
                    if record.get("type") in (1, 28):
                        addresses.append(record["data"])
        if not addresses or not all(_public_address(value) for value in addresses):
            raise ValueError("Downloader destination is not public")
    except (
        httpx.HTTPError,
        AttributeError,
        KeyError,
        TypeError,
        UnicodeError,
        ValueError,
    ) as exc:
        raise ValueError(
            "Downloader destination could not be verified as public"
        ) from exc

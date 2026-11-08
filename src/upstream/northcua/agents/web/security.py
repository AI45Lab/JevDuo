"""Security policies for web-agent navigation and artifacts."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from northcua.core.config import WebAgentConfig


class WebNavigationPolicy:
    """Validate URLs before the web agent opens them."""

    def __init__(self, config: WebAgentConfig) -> None:
        self.config = config

    def validate_url(self, url: str) -> str:
        """Return a validated URL or raise ``ValueError``.

        Args:
            url: Candidate navigation URL from CLI, controller metadata, or
                model tool call.

        Returns:
            Trimmed URL that satisfies configured scheme and network policy.

        Raises:
            ValueError: If the URL is empty, lacks a scheme, uses a disabled
                scheme, or resolves to a blocked local/private address.
        """

        value = url.strip()
        if not value:
            raise ValueError("URL must not be empty.")
        parsed = urlparse(value)
        scheme = parsed.scheme.lower()
        if not scheme:
            raise ValueError("URL must include a scheme, such as https://.")
        if scheme == "file" and not self.config.allow_file_urls:
            raise ValueError("file:// URLs are disabled by web.allow_file_urls.")
        if scheme not in set(self.config.allowed_url_schemes) and not (
            scheme == "file" and self.config.allow_file_urls
        ):
            raise ValueError(f"URL scheme is not allowed: {scheme}")
        if scheme in {"http", "https"}:
            self._validate_http_host(parsed.hostname)
        return value

    def _validate_http_host(self, hostname: str | None) -> None:
        if not hostname:
            raise ValueError("HTTP(S) URL must include a host.")
        host = hostname.lower().strip("[]")
        if host == "localhost" or host.endswith(".localhost"):
            if not self.config.allow_localhost_urls:
                raise ValueError("localhost URLs are disabled by web.allow_localhost_urls.")
            return
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            self._validate_resolved_host(host)
            return
        self._validate_address(address)

    def _validate_resolved_host(self, host: str) -> None:
        try:
            results = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError(f"Could not resolve host for private network validation: {host}") from exc
        for result in results:
            sockaddr = result[4]
            if not sockaddr:
                continue
            try:
                address = ipaddress.ip_address(str(sockaddr[0]))
            except ValueError:
                continue
            self._validate_resolved_address(address)

    def _validate_address(self, address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
        if address.is_loopback:
            if not self.config.allow_localhost_urls:
                raise ValueError("Loopback URLs are disabled by web.allow_localhost_urls.")
            return
        if (
            address.is_private
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ) and not self.config.allow_private_network_urls:
            raise ValueError("Private/internal network URLs are disabled by web.allow_private_network_urls.")

    def _validate_resolved_address(self, address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
        if (
            address.is_loopback
            or address.is_private
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ) and not self.config.allow_private_network_urls:
            raise ValueError("Private/internal network URLs are disabled by web.allow_private_network_urls.")

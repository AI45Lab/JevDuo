"""Fixture staging for case suites."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import os
import socket
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from northcua.experiments.cases.contracts import (
    AppProfileId,
    FixtureSourceKind,
    FixtureSpec,
    LargeArtifactDeliveryEvidence,
    LargeArtifactDeliveryMode,
    MaterializedFixtureReference,
)
from northcua.experiments.cases.materialization.generators import (
    FixtureGeneratorRegistry,
    default_fixture_generator_registry,
)
from northcua.sandboxes.contracts import SandboxFileDeliveryMode, SandboxFileUploadSpec

_MAX_URL_FIXTURE_BYTES = 50 * 1024 * 1024
_VERIFY_PRECOMPUTED_FIXTURE_SHA256_ENV = "NORTHCUA_VERIFY_PRECOMPUTED_FIXTURE_SHA256"
_HostVerification = Literal[
    "",
    "sha256_and_size_verified",
    "endpoint_and_size_verified",
    "endpoint_verified",
]


@dataclass(frozen=True, slots=True)
class _DownloadEndpoint:
    """Resolved URL endpoint that binds safety checks to the actual socket."""

    url: str
    scheme: str
    host: str
    port: int
    address: str
    target: str


class _BoundHTTPConnection(http.client.HTTPConnection):
    """HTTP connection that dials a prevalidated IP for an original host."""

    def __init__(self, endpoint: _DownloadEndpoint, *, timeout: float) -> None:
        super().__init__(endpoint.host, endpoint.port, timeout=timeout)
        self._northcua_address = endpoint.address

    def connect(self) -> None:
        """Open the HTTP socket against the prevalidated address."""

        self.sock = socket.create_connection(
            (self._northcua_address, self.port),
            self.timeout,
            getattr(self, "source_address", None),
        )


class _BoundHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that dials a prevalidated IP while keeping SNI."""

    def __init__(self, endpoint: _DownloadEndpoint, *, timeout: float) -> None:
        context = ssl.create_default_context()
        super().__init__(endpoint.host, endpoint.port, timeout=timeout, context=context)
        self._northcua_address = endpoint.address
        self._northcua_context = context

    def connect(self) -> None:
        """Open the HTTPS socket against the prevalidated address."""

        sock = socket.create_connection(
            (self._northcua_address, self.port),
            self.timeout,
            getattr(self, "source_address", None),
        )
        self.sock = self._northcua_context.wrap_socket(sock, server_hostname=self.host)


@dataclass(frozen=True, slots=True)
class MaterializedFixture:
    """Concrete fixture ready for upload.

    Attributes:
        spec: Fixture spec with the resolved sha256 checksum.
        upload: Provider-neutral upload instruction for the sandbox.
        reference: Typed, JSON-safe materialized fixture reference.
        large_artifact_evidence: Trusted large-artifact delivery binding.
    """

    spec: FixtureSpec
    upload: SandboxFileUploadSpec
    reference: MaterializedFixtureReference | None = None
    large_artifact_evidence: LargeArtifactDeliveryEvidence | None = None


class FixtureMaterializer:
    """Create local fixture files and upload specs."""

    def __init__(self, *, generators: FixtureGeneratorRegistry | None = None) -> None:
        self._generators = generators or default_fixture_generator_registry()

    def materialize(
        self,
        fixture: FixtureSpec,
        *,
        task_id: str,
        fixture_dir: Path,
        spec_dir: Path | None,
        app_profile: AppProfileId | None = None,
    ) -> MaterializedFixture:
        """Materialize one fixture.

        Args:
            fixture: Typed fixture source to stage or reference.
            task_id: Case id used for deterministic generated fixture paths.
            fixture_dir: Local output directory for generated/downloaded files.
            spec_dir: Suite or case directory for resolving relative files.

        Returns:
            Fixture upload record with a verified sha256 checksum.

        Raises:
            ValueError: When an expected checksum does not match.
            FileNotFoundError: When a source fixture file is missing.
        """

        artifact = fixture.large_artifact
        host_verification: _HostVerification
        if artifact is not None and app_profile is None:
            raise ValueError("large artifact materialization requires an intended app_profile")
        if artifact is not None and artifact.delivery == LargeArtifactDeliveryMode.GUEST_DOWNLOAD:
            download_endpoint = self._download_endpoint(fixture.url)
            host_verification = self._preflight_guest_download_source(
                download_endpoint,
                expected_size_bytes=artifact.size_bytes,
            )
            local_path = None
            checksum = artifact.sha256
            size_bytes = artifact.size_bytes
        else:
            download_endpoint = None
            local_path = self._local_path(fixture, task_id=task_id, fixture_dir=fixture_dir, spec_dir=spec_dir)
            if artifact is not None:
                checksum, size_bytes = self._content_address(local_path)
                if checksum != artifact.sha256 or size_bytes != artifact.size_bytes:
                    raise ValueError(
                        f"large artifact content address mismatch for {local_path}: "
                        f"expected {artifact.sha256}/{artifact.size_bytes}, got {checksum}/{size_bytes}"
                    )
                host_verification = "sha256_and_size_verified"
            else:
                checksum = self._checksum_for_fixture(fixture, local_path)
                size_bytes = local_path.stat().st_size
                host_verification = ""
        delivery = (
            SandboxFileDeliveryMode(artifact.delivery.value) if artifact is not None else SandboxFileDeliveryMode.UPLOAD
        )
        upload = SandboxFileUploadSpec(
            local_path=local_path,
            remote_path=fixture.remote_path,
            mode=fixture.mode,
            owner=fixture.owner,
            group=fixture.group,
            parent_mode=fixture.parent_mode,
            parent_owner=fixture.parent_owner,
            parent_group=fixture.parent_group,
            visibility=fixture.visibility.value,
            sha256=checksum,
            size_bytes=size_bytes,
            delivery=delivery,
            source_url=fixture.url if delivery == SandboxFileDeliveryMode.GUEST_DOWNLOAD else "",
            source_host=download_endpoint.host if download_endpoint is not None else "",
            source_port=download_endpoint.port if download_endpoint is not None else 0,
            source_address=download_endpoint.address if download_endpoint is not None else "",
            chunk_size_bytes=(artifact.chunk_size_bytes if artifact is not None else 8 * 1024 * 1024),
            intended_app=app_profile.value if artifact is not None and app_profile is not None else "",
            host_verification=host_verification,
        )
        resolved = fixture.model_copy(update={"sha256": checksum})
        reference = MaterializedFixtureReference(
            local_path=local_path,
            remote_path=resolved.remote_path,
            visibility=resolved.visibility,
            role=resolved.role,
            sha256=checksum,
            size_bytes=size_bytes,
        )
        evidence = None
        if artifact is not None and app_profile is not None:
            assert host_verification
            evidence = LargeArtifactDeliveryEvidence(
                task_id=task_id,
                app_profile=app_profile,
                fixture_remote_path=resolved.remote_path,
                source_kind="url" if download_endpoint is not None else "local_path",
                source=(self._reportable_url_source(fixture.url) if download_endpoint is not None else str(local_path)),
                sha256=checksum,
                size_bytes=size_bytes,
                delivery=artifact.delivery,
                chunk_size_bytes=artifact.chunk_size_bytes,
                host_verification=host_verification,
            )
        return MaterializedFixture(
            spec=resolved,
            upload=upload,
            reference=reference,
            large_artifact_evidence=evidence,
        )

    @staticmethod
    def _preflight_guest_download_source(
        endpoint: _DownloadEndpoint,
        *,
        expected_size_bytes: int,
    ) -> Literal["endpoint_and_size_verified", "endpoint_verified"]:
        """Verify a pinned guest-download endpoint and its size when declared."""

        connection: http.client.HTTPConnection
        if endpoint.scheme == "https":
            connection = _BoundHTTPSConnection(endpoint, timeout=30)
        else:
            connection = _BoundHTTPConnection(endpoint, timeout=30)
        try:
            connection.request(
                "HEAD",
                endpoint.target,
                headers={
                    "Host": endpoint.host,
                    "User-Agent": "NorthCUA-LargeArtifactPreflight/1",
                    "Accept": "*/*",
                },
            )
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise ValueError(f"large artifact source redirects are not allowed: {endpoint.url}")
            if response.status in {405, 501}:
                return "endpoint_verified"
            if response.status >= 400:
                raise ValueError(f"large artifact source preflight failed with HTTP {response.status}: {endpoint.url}")
            content_length = response.getheader("Content-Length")
            if not content_length:
                return "endpoint_verified"
            try:
                declared_bytes = int(content_length)
            except ValueError as exc:
                raise ValueError(f"large artifact source has invalid Content-Length: {endpoint.url}") from exc
            if declared_bytes != expected_size_bytes:
                raise ValueError(
                    f"large artifact source size mismatch for {endpoint.url}: "
                    f"expected {expected_size_bytes}, got {declared_bytes}"
                )
            return "endpoint_and_size_verified"
        finally:
            connection.close()

    @staticmethod
    def _reportable_url_source(url: str) -> str:
        """Return a URL locator without credentials, query parameters, or fragments."""

        parsed = urlparse(url)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        default_port = 443 if parsed.scheme == "https" else 80
        port = f":{parsed.port}" if parsed.port and parsed.port != default_port else ""
        return f"{parsed.scheme}://{host}{port}{parsed.path or '/'}"

    def _local_path(self, fixture: FixtureSpec, *, task_id: str, fixture_dir: Path, spec_dir: Path | None) -> Path:
        if fixture.source_kind == FixtureSourceKind.GENERATED:
            assert fixture.generated is not None
            path = self._staged_path_for_remote(fixture.remote_path, task_id=task_id, fixture_dir=fixture_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._generators.write(fixture.generated, path, task_id=task_id)
            return path
        if fixture.source_kind == FixtureSourceKind.URL:
            endpoint = self._download_endpoint(fixture.url)
            path = self._staged_path_for_remote(fixture.remote_path, task_id=task_id, fixture_dir=fixture_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._download(fixture.url, path, endpoint=endpoint)
            return path
        assert fixture.source_path is not None
        local_path = fixture.source_path.expanduser()
        if not local_path.is_absolute() and spec_dir is not None:
            local_path = spec_dir / local_path
        local_path = local_path.resolve()
        if not local_path.is_file():
            raise FileNotFoundError(f"fixture source file not found: {local_path}")
        return local_path

    @staticmethod
    def _download(url: str, path: Path, *, endpoint: _DownloadEndpoint | None = None) -> None:
        endpoint = endpoint or FixtureMaterializer._download_endpoint(url)
        connection: http.client.HTTPConnection
        if endpoint.scheme == "https":
            connection = _BoundHTTPSConnection(endpoint, timeout=60)
        else:
            connection = _BoundHTTPConnection(endpoint, timeout=60)
        try:
            connection.request(
                "GET",
                endpoint.target,
                headers={
                    "Host": endpoint.host,
                    "User-Agent": "NorthCUA-FixtureMaterializer/1",
                    "Accept": "*/*",
                },
            )
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise ValueError(f"url fixture redirects are not allowed: {url}")
            if response.status >= 400:
                raise ValueError(f"url fixture download failed with HTTP {response.status}: {url}")
            content_length = response.getheader("Content-Length")
            if content_length:
                try:
                    declared_bytes = int(content_length)
                except ValueError:
                    declared_bytes = 0
                if declared_bytes > _MAX_URL_FIXTURE_BYTES:
                    raise ValueError(f"url fixture exceeds {_MAX_URL_FIXTURE_BYTES} bytes: {url}")
            total = 0
            with path.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MAX_URL_FIXTURE_BYTES:
                        raise ValueError(f"url fixture exceeds {_MAX_URL_FIXTURE_BYTES} bytes: {url}")
                    handle.write(chunk)
        finally:
            connection.close()

    @staticmethod
    def _staged_path_for_remote(remote_path: str, *, task_id: str, fixture_dir: Path) -> Path:
        digest = hashlib.sha256(remote_path.encode("utf-8")).hexdigest()[:16]
        name = Path(remote_path).name or "fixture"
        return fixture_dir / task_id / f"{digest}-{name}"

    @staticmethod
    def _download_endpoint(url: str) -> _DownloadEndpoint:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("url fixtures only support http:// and https:// sources")
        host = parsed.hostname or ""
        if not host:
            raise ValueError("url fixture must include a host")
        if parsed.username or parsed.password:
            raise ValueError("url fixture must not embed credentials")
        if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local"):
            raise ValueError("url fixture host must not be local")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError(f"url fixture host could not be resolved: {host}") from exc
        allowed_addresses: list[str] = []
        for item in addresses:
            address = ipaddress.ip_address(item[4][0])
            if (
                not address.is_global
                or address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_reserved
                or address.is_multicast
                or address.is_unspecified
            ):
                raise ValueError(f"url fixture host resolves to a disallowed address: {address}")
            allowed_addresses.append(str(address))
        if not allowed_addresses:
            raise ValueError(f"url fixture host has no usable address: {host}")
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        return _DownloadEndpoint(
            url=url,
            scheme=parsed.scheme,
            host=host,
            port=port,
            address=allowed_addresses[0],
            target=target,
        )

    def _checksum_for_fixture(self, fixture: FixtureSpec, local_path: Path) -> str:
        """Return the fixture checksum without rereading trusted source files.

        Large generated suites carry fixture sha256 values from preflight. Rehashing
        every source file before a live run adds a multi-GB GPFS read pass before
        SQLite/controller startup. Generated and URL fixtures still need a fresh
        checksum because their local content is produced during materialization.
        """

        if (
            fixture.source_kind == FixtureSourceKind.SOURCE
            and fixture.sha256
            and not self._verify_precomputed_fixture_sha256()
        ):
            return fixture.sha256
        else:
            checksum = self._sha256(local_path)
        if fixture.sha256 and fixture.sha256 != checksum:
            raise ValueError(f"fixture checksum mismatch for {local_path}: expected {fixture.sha256}, got {checksum}")
        return checksum

    @staticmethod
    def _verify_precomputed_fixture_sha256() -> bool:
        return os.getenv(_VERIFY_PRECOMPUTED_FIXTURE_SHA256_ENV, "").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _content_address(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size_bytes = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                size_bytes += len(chunk)
        return digest.hexdigest(), size_bytes

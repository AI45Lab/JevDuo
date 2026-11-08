"""Provider registry and adapter protocols for sandbox backends."""

from northcua.sandboxes.provider.base import SandboxProvider
from northcua.sandboxes.provider.registry import SandboxProviderRegistry, default_sandbox_provider_registry

__all__ = ["SandboxProvider", "SandboxProviderRegistry", "default_sandbox_provider_registry"]

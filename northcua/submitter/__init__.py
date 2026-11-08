"""Remote harness submitter clients and typed task specs."""

from northcua.submitter.client import ControllerSubmitterClient
from northcua.submitter.specs import DesktopSubmitSpec, WebSubmitSpec

__all__ = ["ControllerSubmitterClient", "DesktopSubmitSpec", "WebSubmitSpec"]

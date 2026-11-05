"""消息层协议 v0.2：Observation / Action / Receipt。

真源：schema/*.json；标准样例：samples/*.json；运行时校验：validate.py。
"""

from .models import (
    GUI_ACTION_TYPES,
    PROTOCOL_VERSION,
    AccessibilityTree,
    Action,
    BrowserSnapshot,
    Budget,
    CliAction,
    FailAction,
    FinishAction,
    GuiAction,
    Observation,
    Receipt,
    Screenshot,
    WaitAction,
)
from .samples import iter_samples
from .validate import (
    ProtocolValidationError,
    action_from_dict,
    observation_from_dict,
    receipt_from_dict,
)

__all__ = [
    "PROTOCOL_VERSION",
    "GUI_ACTION_TYPES",
    "AccessibilityTree",
    "BrowserSnapshot",
    "Action",
    "Budget",
    "CliAction",
    "FailAction",
    "FinishAction",
    "GuiAction",
    "Observation",
    "Receipt",
    "Screenshot",
    "WaitAction",
    "ProtocolValidationError",
    "action_from_dict",
    "observation_from_dict",
    "receipt_from_dict",
    "iter_samples",
]

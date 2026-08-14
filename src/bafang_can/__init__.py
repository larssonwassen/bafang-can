"""Diagnose, calibrate and configure a Bafang M200 (G210) over CAN.

Built from two upstream projects, both vendored under ``vendor/``:
``andrey-pr/OpenBafangTool`` (MIT) and ``mdi-9/bafang_canable_pro`` (GPLv3).
"""

from .constants import CanOperation, DeviceId
from .frame import BafangId, BafangMessage
from .protocol import BafangClient, BafangError, DeviceError, TimeoutError_
from .system import BafangSystem
from .transport import Adapter, AdapterConfig, find_adapters, open_bus

__version__ = "0.1.0"

__all__ = [
    "Adapter",
    "AdapterConfig",
    "BafangClient",
    "BafangError",
    "BafangId",
    "BafangMessage",
    "BafangSystem",
    "CanOperation",
    "DeviceError",
    "DeviceId",
    "TimeoutError_",
    "find_adapters",
    "open_bus",
]

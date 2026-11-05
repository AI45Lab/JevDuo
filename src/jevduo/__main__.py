"""jevduo CLI。M0 提供 check（协议样例自检）与 version；smoke/run 在后续里程碑加入。"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .protocol import (
    PROTOCOL_VERSION,
    ProtocolValidationError,
    action_from_dict,
    iter_samples,
    observation_from_dict,
    receipt_from_dict,
)

_DISPATCH = {
    "action": action_from_dict,
    "observation": observation_from_dict,
    "receipt": receipt_from_dict,
}


def cmd_check() -> int:
    """校验包内全部 golden 样例，供 CI 与快速自检使用。"""
    passed = 0
    failures: list[str] = []
    for name, data in iter_samples():
        category = name.split("_", 1)[0]
        try:
            _DISPATCH[category](data)
        except ProtocolValidationError as exc:
            failures.append(f"{name}: {exc}")
        else:
            passed += 1
    print(f"协议自检：{passed} 个样例通过，{len(failures)} 个失败（protocol {PROTOCOL_VERSION}）")
    for line in failures:
        print(f"  FAIL {line}", file=sys.stderr)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jevduo", description="JevDuo harness CLI")
    parser.add_argument("--version", action="version", version=f"jevduo {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="校验协议 golden 样例")
    args = parser.parse_args(argv)
    if args.command == "check":
        return cmd_check()
    parser.error(f"未知命令: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())

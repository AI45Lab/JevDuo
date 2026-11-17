"""Command-line composition of the independent loop and its adapters."""

import argparse
import json
import os
from contextlib import closing

from .contracts import RunOptions, Scope
from .desktop import AgentDesktop
from .loop import JevLoop, redact
from .model import TypeSafeModel
from .text_model import ChatTextModel
from .transport import DesktopConfig, LocalTransport, RemoteTransport


def positive_timeout(value):
    if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 300000:
        raise argparse.ArgumentTypeError("--snapshot-timeout-ms must be an integer from 1 to 300000")
    return int(value)


def parser():
    result = argparse.ArgumentParser(description="Drive a native desktop toward a goal with Jev.")
    for name in ("app", "window-id", "vm-url", "bin", "root"):
        result.add_argument(f"--{name}")
    result.add_argument("--snapshot-timeout-ms", type=positive_timeout)
    result.add_argument(
        "--text", action="append", default=[], help="Caller-supplied values, consumed in order"
    )
    result.add_argument(
        "--text-model", help="Model to generate field content when no --text value remains; also TEXT_MODEL"
    )
    result.add_argument("--text-model-url", help="Full Chat Completions endpoint; also TEXT_MODEL_BASE_URL")
    result.add_argument("--cursor", action="store_true")
    result.add_argument(
        "--headed", action="store_true", help="Allow physical text replacement and focus changes"
    )
    result.add_argument("--no-values", action="store_true")
    result.add_argument("--confirm-destructive", action="store_true")
    result.add_argument("--input-mode", choices=("both", "replace", "insert"), default="both")
    result.add_argument(
        "--key", action="append", help="Override the native window key choices; repeat for each combo"
    )
    result.add_argument(
        "--allow-operation", action="append", help="Restrict operation names; repeat as needed"
    )
    result.add_argument(
        "--include-context", action="store_true", help="Include redacted visible text in events"
    )
    result.add_argument("goal", nargs="+", help="Instruction for the whole run")
    return result


def main(argv=None):
    args = parser().parse_intermixed_args(argv)
    try:
        url = args.vm_url or os.environ.get("AGENT_DESKTOP_VM_URL")
        if url and not (args.bin or os.environ.get("AGENT_DESKTOP_BIN")):
            raise ValueError("remote execution requires --bin or AGENT_DESKTOP_BIN naming the VM binary")
        transport = RemoteTransport(url) if url else LocalTransport()
        overrides = {
            key: value
            for key, value in {
                "binary": args.bin,
                "snapshot_timeout_ms": args.snapshot_timeout_ms,
                "headed": args.headed,
            }.items()
            if value is not None
        }
        config = DesktopConfig.from_env(remote=bool(url), overrides=overrides)
        model = TypeSafeModel.from_env()
        text_model = ChatTextModel.from_env(model=args.text_model, endpoint=args.text_model_url)
        desktop = AgentDesktop(config, transport)
        options = RunOptions(
            scope=Scope(args.app, args.root, args.window_id),
            text=args.text,
            cursor=args.cursor,
            values=not args.no_values,
            confirm_destructive=args.confirm_destructive,
            text_model=text_model,
            input_mode=args.input_mode,
            keys=tuple(args.key) if args.key is not None else RunOptions().keys,
            allowed_operations=tuple(args.allow_operation) if args.allow_operation is not None else None,
            include_context=args.include_context,
        )
        status = 1
        with closing(JevLoop(desktop, model).run(" ".join(args.goal), options)) as events:
            for event in events:
                print(json.dumps(event, ensure_ascii=False), flush=True)
                if "stop" in event:
                    status = 0 if event["stop"] == "done" else 1
        return status
    except KeyboardInterrupt:
        print(
            json.dumps({"ok": False, "error": "interrupted; inspect the desktop before retrying"}), flush=True
        )
        return 130
    except Exception as error:
        secrets = {value for value in args.text if value}
        print(json.dumps({"ok": False, "error": redact(str(error), secrets)}, ensure_ascii=False), flush=True)
        return 1

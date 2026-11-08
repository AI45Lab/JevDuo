"""User-facing, credential-free local onboarding fixtures."""

from pathlib import Path


def register_demo_commands(subparsers):
    parser = subparsers.add_parser("demo", help="Serve local tutorial fixtures and a scripted model endpoint")
    commands = parser.add_subparsers(dest="demo_action", required=True)
    serve = commands.add_parser("serve", help="Create tutorial configs and serve fixtures; keep this terminal open")
    serve.add_argument("--directory", default="logs/tutorial")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--controller-port", type=int, default=8765)
    serve.add_argument(
        "--desktop", action="store_true", help="Open a disposable native form (requires Tk and GUI permissions)"
    )
    serve.add_argument(
        "--response-delay", type=float, default=0, help="Delay fixture replies to practice pause/drain/resume"
    )


def run_demo_command(args):
    from northcua.demo.server import serve

    if args.response_delay < 0 or args.response_delay > 10:
        raise ValueError("response-delay must be between 0 and 10 seconds")
    return serve(
        Path(args.directory),
        port=args.port,
        controller_port=args.controller_port,
        desktop=args.desktop,
        delay=args.response_delay,
    )

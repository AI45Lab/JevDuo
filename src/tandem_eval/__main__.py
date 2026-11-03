"""tandem-eval CLI：entry（worker 侧）+ rjob（开发机侧生命周期）。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _rjob_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default="infra/config.json",
                        help="唯一配置文件（全绝对路径，两侧一致）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tandem-eval", description="Tandem 集群测评入口")
    sub = parser.add_subparsers(dest="command", required=True)

    entry = sub.add_parser("entry", help="按 job spec 启动一次测评（worker 内执行）")
    entry.add_argument("--spec", required=True, help="job spec JSON 路径")

    rjob = sub.add_parser("rjob", help="rjob 生命周期（提交/轮询/取日志/收尾）")
    rjob_sub = rjob.add_subparsers(dest="rjob_command", required=True)

    run_cmd = rjob_sub.add_parser("run", help="一站式：sync→spec→submit→watch→fetch→cleanup")
    _rjob_args(run_cmd)
    run_cmd.add_argument("--repo", default=".", help="本地仓库根")
    run_cmd.add_argument("--run-id", required=True)
    run_cmd.add_argument("--benchmark", default="mock", choices=["mock", "osworld1", "osworld2", "osworld21"])
    run_cmd.add_argument("--tasks", default="", help="逗号分隔 task id（覆盖 manifest）")
    run_cmd.add_argument("--limit", type=int, default=None, help="清单截取数量")
    run_cmd.add_argument("--shard", default=None, metavar="I/N",
                         help="把（limit 后的）清单交错切成 N 片，本 run 跑第 I 片（1 起）")
    run_cmd.add_argument("--max-steps", type=int, default=None, help="步数预算")
    run_cmd.add_argument("--max-wall", type=float, default=None, help="墙钟预算（秒）")
    run_cmd.add_argument("--allow-cli", choices=["true", "false"], default=None,
                         help="harness 是否允许 CLI 路由")
    run_cmd.add_argument("--route-policy", choices=["auto", "gui", "cli"], default=None)
    run_cmd.add_argument("--execution-mode", choices=["step", "stage", "goal"], default=None)
    run_cmd.add_argument("--stage-max-actions", type=int, default=None,
                         help="阶段内动作数上限，达到后请求剩余工作的重规划")
    run_cmd.add_argument("--slow-model", default=None, metavar="NAME",
                         help="慢模型（大脑）API 名：config 的 harness.slow_model.apis 里选；默认取 default")
    run_cmd.add_argument("--slow-key-ids", default=None, metavar="1,2,3",
                         help="号池 key 编号；多个编号按 worker 均分，未指定则取配置")
    run_cmd.add_argument("--fast-model", default=None, metavar="NAME",
                         help="快模型（小脑）API 名：config 的 harness.fast_model.apis 里选；默认取 default")
    run_cmd.add_argument("--slow-max-tokens", type=int, default=None,
                         help="慢模型单次响应上限")
    run_cmd.add_argument("--slow-reasoning", choices=["off", "low", "medium", "high", "max"],
                         default=None, help="慢模型思考强度（默认取所选 API 配置）")
    run_cmd.add_argument("--export-paths", default="", help="逗号分隔的 VM 内交付物路径")
    run_cmd.add_argument("--poll", type=float, default=60)
    run_cmd.add_argument("--timeout", type=float, default=None, help="watch 超时秒数")
    run_cmd.add_argument("--keep", action="store_true",
                         help="结束后保留 rjob（不 stop 不 delete），供事后查状态/日志；默认跑完即删")
    run_cmd.add_argument("--delete", action="store_true",
                         help="已默认开启（跑完自动删除 rjob）；保留此参数仅为兼容旧脚本")
    run_cmd.add_argument("--no-sync", action="store_true", help="跳过 rsync（复用部署副本）")

    watch_cmd = rjob_sub.add_parser("watch", help="盯一个已提交的 rjob 至终态并归档")
    _rjob_args(watch_cmd)
    watch_cmd.add_argument("job", help="rjob 名称")
    watch_cmd.add_argument("--run-id", required=True, help="用于定位结果目录")
    watch_cmd.add_argument("--poll", type=float, default=60)
    watch_cmd.add_argument("--timeout", type=float, default=None)
    watch_cmd.add_argument("--keep", action="store_true",
                           help="结束后保留 rjob；默认跑完即删")

    args = parser.parse_args(argv)
    if args.command == "entry":
        from .entry import entry_main

        return entry_main(["--spec", args.spec])

    from .rjob import RjobClient, fetch_artifacts, load_config, run_job, watch

    config = load_config(args.config)
    if args.rjob_command == "run":
        overrides: dict = {"benchmark": args.benchmark}
        if args.slow_key_ids is not None:
            try:
                overrides["slow_key_ids"] = (
                    [int(item.strip()) for item in args.slow_key_ids.split(",")]
                    if args.slow_key_ids.strip() else []
                )
            except ValueError:
                parser.error("--slow-key-ids 需填写正整数编号，多个编号用逗号分隔")
        manifest: dict = {}
        if args.tasks:
            manifest["tasks"] = [item.strip() for item in args.tasks.split(",") if item.strip()]
        if args.limit:
            manifest["limit"] = args.limit
        if args.shard:
            index_text, total_text = args.shard.split("/", 1)
            manifest["shard"] = {"index": int(index_text), "total": int(total_text)}
        if manifest:
            overrides["manifest"] = manifest
        models: dict = {}
        if args.slow_model:
            models["slow"] = args.slow_model
        if args.fast_model:
            models["fast"] = args.fast_model
        if models:
            overrides["models"] = models
        harness_config: dict = {}
        if args.execution_mode is not None:
            harness_config["execution_mode"] = args.execution_mode
        if args.stage_max_actions is not None:
            harness_config["stage_max_actions"] = args.stage_max_actions
        if args.allow_cli is not None:
            harness_config["allow_cli"] = args.allow_cli == "true"
        if args.route_policy:
            harness_config["route_policy"] = args.route_policy
        if args.slow_max_tokens:
            harness_config["slow_max_tokens"] = args.slow_max_tokens
        if args.slow_reasoning:
            harness_config["slow_reasoning_effort"] = args.slow_reasoning
        if harness_config:
            overrides["harness_config"] = harness_config
        budget: dict = {}
        if args.max_steps:
            budget["max_steps"] = args.max_steps
        if args.max_wall:
            budget["max_wall_seconds"] = args.max_wall
        if budget:
            overrides["budget"] = budget
        if args.export_paths:
            overrides["export_paths"] = [
                item.strip() for item in args.export_paths.split(",") if item.strip()
            ]
        result = run_job(
            config=config,
            repo_root=Path(args.repo), run_id=args.run_id, spec_overrides=overrides,
            poll_seconds=args.poll,
            timeout_seconds=args.timeout, keep=args.keep,
            delete=True,  # 跑完自动删除 rjob 是默认行为；--delete 仅为兼容旧脚本而保留
            sync=not args.no_sync,
        )
        print(result["status"])
        if result.get("error"):
            print(f"错误：{result['error']}", file=sys.stderr)
        return 0 if result["status"] == "succeeded" else 1

    client = RjobClient()
    status = watch(client, args.job, poll_seconds=args.poll, timeout_seconds=args.timeout)
    run_root = Path(config["log"]["results_root"]) / args.run_id
    fetch_artifacts(client, args.job, run_root)
    if args.keep:
        print(f"已保留 rjob {args.job}（--keep）", file=sys.stderr)
    else:
        from .rjob import cleanup

        outcome = cleanup(client, args.job)
        print(f"收尾：stopped={outcome['stopped']} deleted={outcome['deleted']}", file=sys.stderr)
    print(status)
    return 0 if status == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())

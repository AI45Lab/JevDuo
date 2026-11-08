"""Build a rerun manifest from non-success cases across prior batches."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from northcua.batch.contracts import BatchManifest, BatchTaskSpec
from northcua.batch.preparation.contracts import PreparedSandboxPolicy
from northcua.experiments.cases.contracts import CaseSpec, CaseSuiteSpec
from northcua.experiments.cases.loader import load_case_suite
from northcua.experiments.contracts import ExperimentControllerSpec
from northcua.sandboxes.contracts import SandboxProviderSpec, SandboxResourceSpec

JsonDict = dict[str, Any]


def build_rerun_manifest(
    *,
    run_root: Path,
    output_manifest: Path,
    rerun_batch_id: str,
    output_case_suite: Path | None = None,
    rerun_suite_id: str = "",
    source_batch_dbs: list[Path] | None = None,
    source_case_suites: list[Path] | None = None,
    new_template: str = "",
    new_template_profile: str = "",
    concurrency: int = 40,
    prepared_sandbox_policy: PreparedSandboxPolicy | None = None,
) -> JsonDict:
    """Collect non-success tasks and write deduplicated rerun artifacts.

    Args:
        run_root: Root directory containing prior batch run outputs.
        output_manifest: Path where the rerun batch manifest is written.
        rerun_batch_id: Batch id for the generated rerun manifest.
        output_case_suite: Optional path for an equivalent case suite.
        rerun_suite_id: Optional suite id for the generated case suite.
        source_batch_dbs: Explicit source batch SQLite files.
        source_case_suites: Explicit source case suite files.
        new_template: Sandbox template alias to use for rerun tasks.
        new_template_profile: Case template profile to use for rerun tasks.
        concurrency: Rerun manifest concurrency.
        prepared_sandbox_policy: Optional prepared sandbox policy for reruns.

    Returns:
        JSON-safe report describing selected sources and generated artifacts.

    Raises:
        FileNotFoundError: If required source artifacts cannot be found.
        ValueError: If no non-success tasks are available for rerun.
    """

    run_root = run_root.expanduser().resolve()
    sources = [path.expanduser().resolve() for path in source_batch_dbs or _discover_batch_dbs(run_root)]
    tasks_by_id: dict[str, BatchTaskSpec] = {}
    cases_by_id: dict[str, CaseSpec] = {}
    latest_non_success_record_by_id: dict[str, JsonDict] = {}
    success_task_ids: set[str] = set()
    source_records: list[JsonDict] = []
    explicit_case_tasks = _case_tasks_from_suites(source_case_suites or [])
    for batch_db in sources:
        manifest_path = _manifest_for_batch_db(batch_db)
        if manifest_path is None:
            continue
        task_by_id = _manifest_tasks_by_id(manifest_path)
        case_suite_path = _case_suite_for_batch_db(batch_db)
        case_by_id = explicit_case_tasks or _case_tasks_from_suites([case_suite_path] if case_suite_path else [])
        for task_id, record in _task_records(batch_db).items():
            enriched_record = {
                **record,
                "batch_db": str(batch_db),
                "manifest": str(manifest_path),
                "case_suite": str(case_suite_path or ""),
            }
            source_records.append(enriched_record)
            if record["status"] == "succeeded":
                success_task_ids.add(task_id)
                tasks_by_id.pop(task_id, None)
                cases_by_id.pop(task_id, None)
                latest_non_success_record_by_id.pop(task_id, None)
                continue
            if task_id in success_task_ids:
                continue
            task_raw = task_by_id.get(task_id)
            if task_raw is None:
                continue
            previous = latest_non_success_record_by_id.get(task_id)
            if previous is not None and float(previous.get("updated_at", 0.0)) > float(record.get("updated_at", 0.0)):
                continue
            tasks_by_id[task_id] = _rewrite_task_raw(
                task_raw,
                new_template=new_template,
                new_template_profile=new_template_profile,
                record=enriched_record,
            )
            case_raw = case_by_id.get(task_id)
            if case_raw is not None:
                cases_by_id[task_id] = _rewrite_case_raw(
                    case_raw,
                    new_template=new_template,
                    new_template_profile=new_template_profile,
                    record=enriched_record,
                )
            latest_non_success_record_by_id[task_id] = record
    tasks = [tasks_by_id[task_id] for task_id in sorted(tasks_by_id)]
    if not tasks:
        raise ValueError("no non-success tasks found in source batch DBs")
    defaults = _defaults_from_first_source(sources)
    if new_template:
        defaults = _r19_sandbox(defaults, template=new_template)
    manifest = BatchManifest(
        batch_id=rerun_batch_id,
        defaults={
            "concurrency": concurrency,
            "sandbox": defaults.model_dump(mode="json"),
        },
        tasks=tasks,
    )
    output_manifest.parent.mkdir(parents=True, exist_ok=True)
    output_manifest.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    if output_case_suite is not None:
        missing_case_ids = sorted(set(tasks_by_id) - set(cases_by_id))
        if missing_case_ids:
            preview = ", ".join(missing_case_ids[:10])
            raise ValueError(f"cannot write case suite; missing source case specs for: {preview}")
        _write_case_suite(
            output_case_suite=output_case_suite,
            rerun_suite_id=rerun_suite_id or output_case_suite.stem,
            rerun_batch_id=rerun_batch_id,
            sources=sources,
            source_records=source_records,
            cases=[cases_by_id[task_id] for task_id in sorted(cases_by_id)],
            new_template=new_template,
            concurrency=concurrency,
            prepared_sandbox_policy=prepared_sandbox_policy or _default_prepared_sandbox_policy(),
        )
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_root": str(run_root),
        "output_manifest": str(output_manifest),
        "output_case_suite": str(output_case_suite or ""),
        "rerun_suite_id": rerun_suite_id or (output_case_suite.stem if output_case_suite else ""),
        "rerun_batch_id": rerun_batch_id,
        "source_batch_db_count": len(sources),
        "source_status_records": len(source_records),
        "source_non_success_records": sum(1 for record in source_records if record["status"] != "succeeded"),
        "source_success_records": sum(1 for record in source_records if record["status"] == "succeeded"),
        "excluded_success_task_count": len(success_task_ids),
        "deduped_task_count": len(tasks),
        "new_template": new_template,
        "new_template_profile": new_template_profile,
        "concurrency": concurrency,
        "sources": source_records,
    }
    output_manifest.with_suffix(output_manifest.suffix + ".report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return report


def _rewrite_task_raw(
    task: JsonDict,
    *,
    new_template: str,
    new_template_profile: str,
    record: JsonDict,
) -> BatchTaskSpec:
    sandbox_raw = task.get("sandbox")
    sandbox = SandboxProviderSpec.model_validate(sandbox_raw) if isinstance(sandbox_raw, dict) else None
    if new_template:
        sandbox = _r19_sandbox(sandbox or SandboxProviderSpec(), template=new_template)
    metadata_raw = task.get("metadata")
    metadata = dict(metadata_raw) if isinstance(metadata_raw, dict) else {}
    if new_template_profile:
        metadata["template_profile"] = new_template_profile
    metadata.update(
        _rerun_metadata(record=record, new_template=new_template, new_template_profile=new_template_profile)
    )
    return BatchTaskSpec.model_validate(
        {**task, "sandbox": sandbox.model_dump(mode="json") if sandbox is not None else None, "metadata": metadata}
    )


def _rewrite_case_raw(
    case: CaseSpec,
    *,
    new_template: str,
    new_template_profile: str,
    record: JsonDict,
) -> CaseSpec:
    metadata = {
        **case.metadata,
        **_rerun_metadata(record=record, new_template=new_template, new_template_profile=new_template_profile),
    }
    updates: JsonDict = {"metadata": metadata}
    if new_template_profile:
        updates["template_profile"] = new_template_profile
    tags = list(case.tags)
    if "rerun.non_success" not in tags:
        tags.append("rerun.non_success")
        updates["tags"] = tags
    return CaseSpec.model_validate({**case.model_dump(mode="json"), **updates})


def _rerun_metadata(*, record: JsonDict, new_template: str, new_template_profile: str) -> JsonDict:
    return {
        "rerun_source": "non_success_collection",
        "rerun_source_status": str(record.get("status", "")),
        "rerun_source_updated_at": record.get("updated_at", 0.0),
        "rerun_source_batch_db": str(record.get("batch_db", "")),
        "rerun_source_manifest": str(record.get("manifest", "")),
        "rerun_source_case_suite": str(record.get("case_suite", "")),
        "template_alias": new_template,
        "template_profile": new_template_profile,
    }


def _discover_batch_dbs(run_root: Path) -> list[Path]:
    return sorted(path for path in run_root.rglob("batch_state.sqlite") if path.is_file())


def _manifest_for_batch_db(batch_db: Path) -> Path | None:
    candidates = [
        batch_db.parent / "manifest.json",
        batch_db.parent.parent / "manifest.json",
        batch_db.parent.parent.parent / "manifest.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _case_suite_for_batch_db(batch_db: Path) -> Path | None:
    candidates = [
        batch_db.parent / "case_suite.json",
        batch_db.parent.parent / "case_suite.json",
        batch_db.parent.parent.parent / "case_suite.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _manifest_tasks_by_id(manifest_path: Path) -> dict[str, JsonDict]:
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    tasks = raw.get("tasks", [])
    if not isinstance(tasks, list):
        return {}
    result: dict[str, JsonDict] = {}
    for task in tasks:
        if not isinstance(task, dict):
            continue
        task_id = task.get("task_id")
        if isinstance(task_id, str) and task_id:
            result[task_id] = dict(task)
    return result


def _case_tasks_from_suites(paths: list[Path]) -> dict[str, CaseSpec]:
    result: dict[str, CaseSpec] = {}
    for path in paths:
        if path is None:
            continue
        resolved = path.expanduser().resolve()
        if not resolved.exists():
            continue
        suite = load_case_suite(resolved)
        for task in suite.tasks:
            result[task.task_id] = task
    return result


def _task_records(batch_db: Path) -> dict[str, JsonDict]:
    with sqlite3.connect(str(batch_db)) as connection:
        connection.row_factory = sqlite3.Row
        if not _table_exists(connection, "batch_tasks"):
            return {}
        rows = connection.execute(
            "SELECT task_id, status, updated_at FROM batch_tasks ORDER BY updated_at ASC",
        ).fetchall()
    return {
        str(row["task_id"]): {
            "task_id": str(row["task_id"]),
            "status": str(row["status"]),
            "updated_at": float(row["updated_at"] or 0.0),
        }
        for row in rows
    }


def _defaults_from_first_source(sources: list[Path]) -> SandboxProviderSpec:
    for source in sources:
        manifest_path = _manifest_for_batch_db(source)
        if manifest_path is None:
            continue
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        defaults = raw.get("defaults")
        sandbox = defaults.get("sandbox") if isinstance(defaults, dict) else None
        if isinstance(sandbox, dict):
            return SandboxProviderSpec.model_validate(sandbox)
    return SandboxProviderSpec()


def _suite_from_first_source(sources: list[Path]) -> CaseSuiteSpec:
    for source in sources:
        suite_path = _case_suite_for_batch_db(source)
        if suite_path is not None and suite_path.exists():
            return load_case_suite(suite_path)
    return CaseSuiteSpec(tasks=[CaseSpec(task_id="placeholder", query="placeholder", app_profile="desktop.generic")])


def _write_case_suite(
    *,
    output_case_suite: Path,
    rerun_suite_id: str,
    rerun_batch_id: str,
    sources: list[Path],
    source_records: list[JsonDict],
    cases: list[CaseSpec],
    new_template: str,
    concurrency: int,
    prepared_sandbox_policy: PreparedSandboxPolicy,
) -> None:
    source_suite = _suite_from_first_source(sources)
    sandbox = source_suite.sandbox
    if new_template:
        sandbox = _r19_sandbox(sandbox, template=new_template, suite_id=rerun_suite_id)
    metadata = {
        **source_suite.metadata,
        "rerun_source": "non_success_collection",
        "rerun_batch_id": rerun_batch_id,
        "source_batch_dbs": [str(source) for source in sources],
        "source_status_records": len(source_records),
        "source_non_success_records": sum(1 for record in source_records if record["status"] != "succeeded"),
        "source_success_records": sum(1 for record in source_records if record["status"] == "succeeded"),
    }
    suite = CaseSuiteSpec.model_validate(
        {
            **source_suite.model_dump(mode="json"),
            "suite_id": rerun_suite_id,
            "description": f"Rerun non-success cases for {rerun_batch_id} on the updated E2B template.",
            "controller": _portable_controller(source_suite.controller).model_dump(mode="json"),
            "sandbox": sandbox.model_dump(mode="json"),
            "prepared_sandbox_policy": prepared_sandbox_policy.model_dump(mode="json"),
            "concurrency": concurrency,
            "case_sources": [],
            "tasks": [case.model_dump(mode="json") for case in cases],
            "metadata": metadata,
        }
    )
    output_case_suite.parent.mkdir(parents=True, exist_ok=True)
    output_case_suite.write_text(
        yaml.safe_dump(suite.model_dump(mode="json"), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _portable_controller(controller: ExperimentControllerSpec) -> ExperimentControllerSpec:
    return controller.model_copy(update={"db_path": None, "blob_dir": None, "artifact_root_dir": None})


def _default_prepared_sandbox_policy() -> PreparedSandboxPolicy:
    return PreparedSandboxPolicy(
        enabled=True,
        depth="full_init",
        buffer=50,
        prepare_concurrency=10,
        ttl_seconds=5400,
        initial_warmup_wait_seconds=PreparedSandboxPolicy().initial_warmup_wait_seconds,
        claim_wait_seconds=PreparedSandboxPolicy().claim_wait_seconds,
        claim_pending_wait_seconds=PreparedSandboxPolicy().claim_pending_wait_seconds,
    )


def _r19_sandbox(base: SandboxProviderSpec, *, template: str, suite_id: str = "") -> SandboxProviderSpec:
    metadata = dict(base.metadata)
    if suite_id:
        metadata["suite"] = suite_id
    metadata["osworld_parity_template"] = template
    return SandboxProviderSpec.model_validate(
        {
            **base.model_dump(mode="json"),
            "provider": "e2b",
            "template": template,
            "sandbox_id": "",
            "desktop": True,
            "platform": "linux",
            "resources": SandboxResourceSpec(
                cpu_count=8,
                memory_mb=16384,
                timeout_seconds=7200,
            ).model_dump(mode="json"),
            "resolution": [1920, 1080],
            "device_python": "python3.12",
            "metadata": metadata,
        }
    )


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row is not None


def build_parser() -> argparse.ArgumentParser:
    """Build the rerun-suite command parser.

    Returns:
        Configured argument parser.
    """

    parser = argparse.ArgumentParser(description="Build a non-success rerun batch manifest")
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--output-manifest", required=True, type=Path)
    parser.add_argument("--output-case-suite", default=None, type=Path)
    parser.add_argument("--rerun-suite-id", default="")
    parser.add_argument("--rerun-batch-id", default="")
    parser.add_argument("--source-batch-db", action="append", default=[], type=Path)
    parser.add_argument("--source-case-suite", action="append", default=[], type=Path)
    parser.add_argument("--new-template", required=True)
    parser.add_argument("--new-template-profile", default="")
    parser.add_argument("--concurrency", type=int, default=40)
    parser.add_argument("--prepare-buffer", type=int, default=50)
    parser.add_argument("--prepare-concurrency", type=int, default=10)
    parser.add_argument("--prepare-ttl-seconds", type=float, default=5400)
    parser.add_argument(
        "--prepare-initial-warmup-wait-seconds",
        type=float,
        default=PreparedSandboxPolicy().initial_warmup_wait_seconds,
    )
    parser.add_argument(
        "--prepare-claim-wait-seconds",
        type=float,
        default=PreparedSandboxPolicy().claim_wait_seconds,
    )
    parser.add_argument(
        "--prepare-claim-pending-wait-seconds",
        type=float,
        default=PreparedSandboxPolicy().claim_pending_wait_seconds,
    )
    parser.add_argument(
        "--prepare-min-assignment-ttl-seconds",
        type=float,
        default=PreparedSandboxPolicy().min_assignment_ttl_seconds,
    )
    parser.add_argument(
        "--prepare-assignment-ttl-margin-seconds",
        type=float,
        default=PreparedSandboxPolicy().assignment_ttl_margin_seconds,
    )
    parser.add_argument("--prepare-depth", default="full_init", choices=["fixture_setup", "full_init"])
    parser.add_argument("--disable-prepared", action="store_true")
    return parser


def main() -> None:
    """Run the rerun-suite CLI entrypoint and print its JSON report."""

    args = build_parser().parse_args()
    rerun_batch_id = args.rerun_batch_id or f"rerun-non-success-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    prepared_policy = PreparedSandboxPolicy(
        enabled=not args.disable_prepared,
        depth=args.prepare_depth,
        buffer=args.prepare_buffer,
        prepare_concurrency=args.prepare_concurrency,
        ttl_seconds=args.prepare_ttl_seconds,
        initial_warmup_wait_seconds=args.prepare_initial_warmup_wait_seconds,
        claim_wait_seconds=args.prepare_claim_wait_seconds,
        claim_pending_wait_seconds=args.prepare_claim_pending_wait_seconds,
        min_assignment_ttl_seconds=args.prepare_min_assignment_ttl_seconds,
        assignment_ttl_margin_seconds=args.prepare_assignment_ttl_margin_seconds,
    )
    report = build_rerun_manifest(
        run_root=args.run_root,
        output_manifest=args.output_manifest,
        rerun_batch_id=rerun_batch_id,
        output_case_suite=args.output_case_suite,
        rerun_suite_id=args.rerun_suite_id,
        source_batch_dbs=args.source_batch_db or None,
        source_case_suites=args.source_case_suite or None,
        new_template=args.new_template,
        new_template_profile=args.new_template_profile,
        concurrency=args.concurrency,
        prepared_sandbox_policy=prepared_policy,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":  # pragma: no cover
    main()

"""Fixture generation and upload planning for experiment tasks."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from northcua.experiments.contracts import ExperimentSpec, ExperimentTaskSpec, SandboxFileInjectionSpec
from northcua.sandboxes.contracts import SandboxFileUploadSpec


@dataclass(frozen=True, slots=True)
class MaterializedTask:
    """Task plus concrete uploads generated for its sandbox.

    Attributes:
        task: Experiment task definition after profile resolution.
        uploads: Concrete host-to-sandbox files ready for provider upload.
    """

    task: ExperimentTaskSpec
    uploads: tuple[SandboxFileUploadSpec, ...]


@dataclass(frozen=True, slots=True)
class MaterializedExperiment:
    """Experiment spec with generated fixtures written to disk.

    Attributes:
        spec: Original experiment definition.
        output_dir: Concrete experiment output directory.
        fixture_dir: Directory containing generated fixture files.
        tasks: Materialized task records in experiment order.
    """

    spec: ExperimentSpec
    output_dir: Path
    fixture_dir: Path
    tasks: tuple[MaterializedTask, ...]


class ExperimentMaterializer:
    """Create deterministic fixture files and typed sandbox upload specs."""

    def materialize(
        self,
        spec: ExperimentSpec,
        *,
        output_dir: Path,
        spec_dir: Path | None = None,
    ) -> MaterializedExperiment:
        """Materialize generated and source fixtures for an experiment.

        Args:
            spec: Experiment suite definition.
            output_dir: Concrete experiment output directory.
            spec_dir: Directory used to resolve relative source fixture paths.

        Returns:
            Materialized experiment with one upload list per task.
        """

        fixture_dir = output_dir / "fixtures"
        fixture_dir.mkdir(parents=True, exist_ok=True)
        tasks: list[MaterializedTask] = []
        for task in spec.tasks:
            materialized = tuple(
                self._materialize_injection(
                    injection,
                    task=task,
                    fixture_dir=fixture_dir,
                    spec_dir=spec_dir,
                )
                for injection in task.fixtures
            )
            uploads = tuple(upload for upload, _injection in materialized)
            fixtures = [injection for _upload, injection in materialized]
            tasks.append(MaterializedTask(task=task.model_copy(update={"fixtures": fixtures}), uploads=uploads))
        materialized_spec = spec.model_copy(update={"tasks": [item.task for item in tasks]})
        return MaterializedExperiment(
            spec=materialized_spec,
            output_dir=output_dir,
            fixture_dir=fixture_dir,
            tasks=tuple(tasks),
        )

    def _materialize_injection(
        self,
        injection: SandboxFileInjectionSpec,
        *,
        task: ExperimentTaskSpec,
        fixture_dir: Path,
        spec_dir: Path | None,
    ) -> tuple[SandboxFileUploadSpec, SandboxFileInjectionSpec]:
        if injection.generated_name:
            local_path = self._write_generated_fixture(
                task=task, fixture_dir=fixture_dir, name=injection.generated_name
            )
        else:
            assert injection.source_path is not None
            local_path = injection.source_path.expanduser()
            if not local_path.is_absolute() and spec_dir is not None:
                local_path = spec_dir / local_path
            local_path = local_path.resolve()
        checksum = self._sha256(local_path)
        if injection.sha256 and injection.sha256 != checksum:
            raise ValueError(f"fixture checksum mismatch for {local_path}: expected {injection.sha256}, got {checksum}")
        upload = SandboxFileUploadSpec(
            local_path=local_path,
            remote_path=injection.remote_path,
            mode=injection.mode,
            sha256=checksum,
        )
        materialized_injection = injection.model_copy(update={"sha256": checksum})
        return upload, materialized_injection

    def _write_generated_fixture(self, *, task: ExperimentTaskSpec, fixture_dir: Path, name: str) -> Path:
        path = fixture_dir / task.task_id / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if name.endswith(".html"):
            path.write_text(_chrome_html(task_id=task.task_id), encoding="utf-8")
        elif name.endswith(".csv"):
            path.write_text(_calc_csv(), encoding="utf-8")
        elif name.endswith(".ppm"):
            path.write_text(_gimp_ppm(), encoding="ascii")
        else:
            raise ValueError(f"no generator registered for fixture: {name}")
        return path

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


def _chrome_html(*, task_id: str) -> str:
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>NorthCUA Golden Chrome Fixture</title>
    <style>
      body {{ font-family: sans-serif; margin: 48px; background: #f6f7f9; color: #20242a; }}
      main {{ max-width: 720px; padding: 32px; background: white; border: 1px solid #d0d7de; }}
      h1 {{ font-size: 40px; margin: 0 0 16px; }}
    </style>
  </head>
  <body>
    <main>
      <h1>NorthCUA Golden Chrome Ready</h1>
      <p id="task-id">Task: {task_id}</p>
    </main>
  </body>
</html>
"""


def _calc_csv() -> str:
    return "item,value\nalpha,12\nbeta,18\ngamma,30\ntotal,\n"


def _gimp_ppm() -> str:
    # Portable pixmap that GIMP can open without optional Python image
    # dependencies. Keep the canvas large enough to be unambiguous in a
    # 1280x900 E2B screenshot; a 12x12 source renders as a nearly invisible
    # thumbnail at GIMP's default 100% zoom.
    width = 128
    height = 128
    tile_size = 16
    rows: list[str] = []
    for y in range(height):
        values: list[str] = []
        for x in range(width):
            if ((x // tile_size) + (y // tile_size)) % 2 == 0:
                values.extend(["220", "30", "70"])
            else:
                values.extend(["40", "120", "220"])
        rows.append(" ".join(values))
    return f"P3\n{width} {height}\n255\n" + "\n".join(rows) + "\n"

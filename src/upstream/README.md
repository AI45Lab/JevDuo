<div align="center">

<a href="docs/assets/brand/northcua-logo.svg#gh-light-mode-only">
  <img src="docs/assets/brand/northcua-logo.svg#gh-light-mode-only" width="420" height="112" alt="NorthCUA">
</a>
<a href="docs/assets/brand/northcua-logo-dark.svg#gh-dark-mode-only">
  <img src="docs/assets/brand/northcua-logo-dark.svg#gh-dark-mode-only" width="420" height="112" alt="NorthCUA">
</a>

**Worlds in mind. Wonders at your fingertips.**

**Turn intent into work — across Desktop and Web.**

An open-source computer-use harness. Bring your model. Run a task. Inspect the result.

[**Get started**](docs/first-run.md) · [**Watch the work**](#see-computer-use-in-action) · [**Technical Report**](https://arxiv.org/pdf/2611.02102v1) · [**Models**](docs/models.md) · [**Documentation**](docs/README.md) · [**中文**](README.zh-CN.md)

[![CI](https://github.com/beipo-ai/NorthCUA/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/beipo-ai/NorthCUA/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-0057B8?style=flat-square)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?style=flat-square&logo=python&logoColor=white)](pyproject.toml)
[![Built on NexAU](https://img.shields.io/badge/Built%20on-NexAU-0B8F87?style=flat-square)](https://github.com/hzhua/nexau/tree/28511d9f0362191a6c378fd441b2be55309a9e32)

[![Hugging Face: NorthCUA-Pro](https://img.shields.io/badge/Hugging%20Face-NorthCUA--Pro-FFD21E?style=flat-square&logo=huggingface&logoColor=black)](https://huggingface.co/china-beipo/NorthCUA-Pro)
[![arXiv: 2611.02102](https://img.shields.io/badge/arXiv-2611.02102-b31b1b?style=flat-square)](https://arxiv.org/pdf/2611.02102v1)
[![Agent Skill](https://img.shields.io/badge/Agent%20Skill-From%20zero%20to%20tasks-0B8F87?style=flat-square)](skills/northcua/SKILL.md)
[![Contributions welcome](https://img.shields.io/badge/PRs-welcome-6554C0?style=flat-square)](CONTRIBUTING.md)

</div>

From an interactive 3D heart to a native Blender animation, computer use is more
than the next click. **NorthCUA Harness** connects actions, visual context,
working notes, live outcome checks and recoverable batches — so you can follow
the whole task, from the first observation to the final artifact.

This repository releases the Harness from the [NorthCUA Technical Report](https://arxiv.org/pdf/2611.02102v1).
Built on public [NexAU](https://github.com/hzhua/nexau), it is model-agnostic:
connect a vision model with structured tool calls, or try the no-key local
tutorial first. **Start on your machine. Scale to a batch. Keep the evidence.**

## See computer use in action

<table>
<tr>
<td width="50%" valign="top">

### Ruby Heart · 30 seconds

**An idea becomes an interactive 3D heart.**<br>
GeoGebra · controls, construction, presentation and verification.

https://github.com/user-attachments/assets/8afee957-87d7-43c9-8dff-21de7478dabf

[Download MP4](https://github.com/beipo-ai/NorthCUA/raw/refs/heads/main/docs/assets/demos/ruby-heart-30s.mp4) · [Result frame](docs/assets/demos/ruby-heart.png)

</td>
<td width="50%" valign="top">

### Blender UFO · 30 seconds

**Sketch it. Model it. Make it move.**<br>
Inkscape → Blender · an editable craft and native animation.

https://github.com/user-attachments/assets/b959f684-a23a-4052-bfcb-45d8c0c8c546

[Download MP4](https://github.com/beipo-ai/NorthCUA/raw/refs/heads/main/docs/assets/demos/blender-ufo-30s.mp4) · [Result frame](docs/assets/demos/blender-ufo.png)

</td>
</tr>
</table>

Two **30-second accelerated edits** from the broader NorthCUA project. Playback
duration is not task execution time. MP4s are bundled for offline viewing;
GitHub's native players appear above. These are project showcases, not reruns
or checkpoint benchmarks of this release. [Details and provenance](docs/showcase.md).

## A harness for the whole task

[![NorthCUA lifecycle: prepare, execute with context and notes, evaluate the live outcome, preserve evidence and release resources](docs/assets/harness-lifecycle.png)](docs/system-tour.md)

*Figure 4 of the current report. Mechanism illustration; executable checks are recorded separately.*

| What you need | What NorthCUA provides |
|---|---|
| **Act on Desktop and Web** | Native screenshot/mouse/keyboard tools, or browser screenshots, DOM references, forms and tabs, with shared session and step contracts. |
| **Stay on track over long tasks** | A bounded overlay/clean image window, task-preserving compaction and durable working notes, with complete traces retained outside prompt history. |
| **Verify what actually happened** | An independent evaluator can inspect artifacts and the original live workspace before cleanup; actor completion and evaluation verdict stay separate. |
| **Run many tasks reliably** | Controller/device execution, isolated attempts, bounded retries, persistent batch state, resume, outcome records and confirmed cleanup. |

[System tour](docs/system-tour.md) · [Paper ↔ code map](docs/paper-map.md) · [Architecture](docs/architecture.md)

## Quick start

Use **Python 3.12**, Git and [uv 0.13.6+](https://docs.astral.sh/uv/).
The commands below work in Bash and PowerShell:

**Dependency note:** the pinned public NexAU requires Transformers 4.x. Known
upstream advisories remain under a [disclosed, expiring risk policy](docs/security/dependencies.md).
Use trusted model/tokenizer configuration; run model serving separately.

```sh
git clone https://github.com/beipo-ai/NorthCUA.git
cd NorthCUA
uv sync --locked --python 3.12 --extra local --extra device
uv run --no-sync playwright install chromium
uv run --no-sync northcua --help
```

On Linux, install browser libraries with `uv run --no-sync playwright install --with-deps chromium`.

### First result, without an API key

In **terminal 1**, start the bundled tutorial service and leave it running:

```sh
uv run --no-sync northcua demo serve --directory logs/tutorial
```

After it prints `"ready": true`, open **terminal 2** in the same checkout:

```sh
uv run --no-sync northcua config validate --app-config logs/tutorial/app.example.yaml
uv run --no-sync northcua local web run --app-config logs/tutorial/app.example.yaml --url http://127.0.0.1:8787/task/first-web --query 'Task ID: first-web. Activate the button and verify the receipt.' --session-id first-web --max-steps 6 --evaluate
```

This uses a **real browser and a deterministic local model endpoint**. Look for
exit code 0, an evaluator verdict of `success` and the task's receipt in
`logs/tutorial/events.jsonl`. The result is saved to
`logs/tutorial/artifacts/first-web/result.json` (`evaluation_report.overall_verdict`).
It checks integration, not autonomous model quality.
Next, run the [three-task local batch and resume tutorial](docs/local-batch.md).
Stop the service with Ctrl+C when finished. For another run, choose a fresh output
directory; the tutorial preserves existing results.

<details>
<summary><strong>Use your own model — Desktop or Web</strong></summary>

A model must accept images and structured tool calls. [Model setup](docs/models.md)
covers Chat, Responses, Anthropic and Gemini. On macOS/Linux with Bash or zsh:

```sh
cp .env.example .env
# Edit .env: set the model ID and API key; change the base URL if needed.
set -a
. ./.env
set +a
uv run --no-sync northcua config init --directory logs/my-config --model "$NORTHCUA_MODEL" --base-url "$NORTHCUA_BASE_URL"
```

For Windows, use the [PowerShell configuration recipe](docs/first-run.md#2a-use-your-model-edit-only-model-connection-settings).
The CLI does not automatically load `.env`; keep keys out of tracked files.

Validate your generated configuration:

```sh
uv run --no-sync northcua config validate --app-config logs/my-config/app.example.yaml
```

Then run one Web task:

```sh
uv run --no-sync northcua local web run --app-config logs/my-config/app.example.yaml --url https://example.com --query 'Report the main heading.' --max-steps 8
```

Desktop tools return screenshots directly. For a model that emits **0–1000 relative
coordinates**, including the NorthCUA model family, first set
`desktop.coordinate_policy.input_space: normalized_1000` in the app YAML;
the Harness maps them to execution pixels. The default remains
`image_pixels`. Web point tools use CSS pixels and DOM refs stay element IDs.
[Copyable coordinate settings](docs/configuration.md#desktop-coordinates).

Then use an interactive desktop with the [required platform permissions](docs/getting-started.md#run-desktop):

```sh
uv run --no-sync northcua local desktop run --app-config logs/my-config/app.example.yaml --query 'Open a calculator and compute 12 * 7.' --max-steps 15
```

</details>

**Using a coding agent?** Load the [NorthCUA operator skill](skills/northcua/SKILL.md).
It routes the agent through installation, model settings, tasks, evidence,
batch execution and recovery. [From-zero guide](docs/first-run.md).

## Choose your workflow

| Goal | Guide |
|---|---|
| From zero to a checked result | [First run](docs/first-run.md) |
| Three local tasks, then resume | [Local batch](docs/local-batch.md) |
| Python, CLI, WebSocket or controller/device | [Execution modes](docs/running.md) |
| Context, images, notes and compaction | [Context and memory](docs/context.md) |
| Evaluation, artifacts and trace viewer | [Outcome evaluation](docs/evaluation.md) |
| Recommended Desktop settings | [Model, context and retry recipes](docs/desktop-settings.md) |
| Extend, test or contribute | [Architecture](docs/architecture.md) · [Testing](docs/testing.md) · [Contributing](CONTRIBUTING.md) |

## Research, models and availability

The **[NorthCUA Technical Report on arXiv](https://arxiv.org/pdf/2611.02102v1)**
describes the broader NorthCUA system and reports its model results. A
[123-page snapshot dated 31 October 2026](docs/paper/main.pdf) is bundled for
offline reading. This repository publishes the **Desktop/Web Harness**.
Experience Flywheel, data generation, staged training and model weights are
separate research components, not hidden install requirements.

The NorthCUA model family is published separately on Hugging Face:

| Model | Total / active parameters | Architecture | Weights and card |
|---|---|---|---|
| **NorthCUA-Nano** | 9B / 9B | Dense | [Hugging Face](https://huggingface.co/china-beipo/NorthCUA-Nano) |
| **NorthCUA-Mini** | 35B / 3B | Mixture of experts | [Hugging Face](https://huggingface.co/china-beipo/NorthCUA-Mini) |
| **NorthCUA-Pro** | 397B / 17B | Mixture of experts | [Hugging Face](https://huggingface.co/china-beipo/NorthCUA-Pro) |

*Nominal model-card/report counts. Active parameters describe per-token
computation, not the memory needed to load the weights.*

[**NorthCUA-Pro weights and model card**](https://huggingface.co/china-beipo/NorthCUA-Pro)
are publicly accessible under the model card's Apache-2.0 license as checked on
**3 November 2026**. The report's original model link resolves to this resource.
The checkpoint is separate from this Harness; its inference and benchmark
capability have not been accepted by this repository's integration tests.
[Revision, configuration and serving considerations](docs/models.md).

**What has been checked:** the [release verification](docs/release-0.2.0.md) records
fresh installation, regression tests, real-browser tasks, local batches, installed
wheels and independent reviews. Scripted-model integration is distinct from
hosted-model capability and full OSWorld/cloud acceptance. PyPI publication is
not required or assumed by these source-install commands.

## Contribute and cite

Bug reports and small, reproducible contributions are welcome. Start with
[CONTRIBUTING.md](CONTRIBUTING.md) and the [clean scope](docs/clean-scope.md).
For reproducible research, record your code/NexAU revisions, model settings,
task source, observation/action profile and evaluation protocol.

```bibtex
@article{zhang2026northcua,
  title={NorthCUA Technical Report: Turbocharging Computer-Use Agents through Experience at Scale},
  author={Zhang, Chaoyun and Li, Bingxuan and Guo, Pinxue and Gao, Yangfan and Cai, Zikun and Xie, Tianci and Li, Hui and Huang, Yuqi and Cao, Yifei and Mao, Bo and Tang, Tian and Zheng, Qichang and Liu, Tongtong and Wang, Xiaoxing and Liu, Jinxiu and Lin, Lizhi and Shao, Zhou and Zhou, Maosen and Zheng, Miao and Zhang, Shuo and He, Yangji and Lei, Kepeng and Zhao, Ning and Shen, Wei and Jin, Zhenjiang and Zhang, Xinyue and Sun, Peng and Han, Zhenhua and Zheng, Rui and Yan, Hang},
  journal={arXiv preprint arXiv:2611.02102},
  year={2026},
  url={https://arxiv.org/pdf/2611.02102v1}
}
```

[BibTeX file](CITATION.bib) · [Citation metadata](CITATION.cff) · [Apache-2.0](LICENSE) · [Third-party notices](THIRD_PARTY_NOTICES.md) · [Security](SECURITY.md) · [Community](CODE_OF_CONDUCT.md) · [Changelog](CHANGELOG.md)

## Contact

For questions about NorthCUA, research discussions or collaboration:

- [zhangchaoyun@qijizhifeng.com](mailto:zhangchaoyun@qijizhifeng.com)
- [libingxuan@qijizhifeng.com](mailto:libingxuan@qijizhifeng.com)

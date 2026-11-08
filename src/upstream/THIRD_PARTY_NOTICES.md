# Third-party materials and notices

NorthCUA Harness code is distributed under [Apache-2.0](LICENSE).
[NOTICE](NOTICE) identifies the project. This does not grant rights to model
weights, third-party software or services merely referenced by the project.

| Material | Attribution and scope |
|---|---|
| NexAU | Public [hzhua/nexau](https://github.com/hzhua/nexau), pinned by full commit in `pyproject.toml`/`uv.lock`; upstream retains its own license and notices. Not vendored here. |
| Python dependencies | Installed separately from their upstream sources according to the lockfile; each retains its own license. No installed virtual environment is part of the source distribution. |
| NorthCUA report and mechanism figures | Maintainer-provided NorthCUA Team research materials. The exact report and figure sources/checksums are identified in `docs/paper/manifest.json` and `docs/assets/manifest.json`. Figures are mechanism illustrations, not recorded task outcomes. |
| TeX Gyre Adventor logo lettering | Exact outlined glyphs from the report's embedded font; no font program is shipped. GUST Font License and referenced LPPL 1.3c are retained in `docs/assets/brand/`. See that directory's provenance and license files. |
| Blue Beipo mark | Maintainer-supplied original mark, copied unchanged from the report's branding source. Its digest is recorded in the brand manifest. |
| Recorded workflow media | Maintainer-supplied project showcases with provenance in `docs/assets/demos/manifest.json`; not a model benchmark or a bundled third-party application. UI screenshots may show third-party names, trademarks and application interfaces. |

Models and datasets referenced in the report are not distributed by this
repository. Obtain their licenses and permissions from their publishers;
the NorthCUA-Pro model's published license and revision are documented in
[the model guide](docs/models.md), independently from this code license.

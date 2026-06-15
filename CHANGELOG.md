# Changelog

All notable changes to herdr-flist. Versions follow the manifest `version` field
in `herdr-plugin.toml`.

## 0.1.2

- Docs: sync README and PLUGINS.md to the current behavior (right sidebar,
  `uv run`, `HERDR_FILELIST_WIDTH` tunable). No behavior change.
- New `docs/img/filelist-sidebar.png` composite showing the docked-sidebar
  layout; `_screenshot.py` gains a `--composite` mode for it.

## 0.1.1

- Run the pane via `uv run filelist.py` with a PEP 723 inline-metadata block
  (`requires-python >= 3.9`, no dependencies). A bare `python3 filelist.py`
  remains an acceptable fallback since the block is a comment to the interpreter.

## 0.1.0

- Open as a **right sidebar column** by default (`--direction right`) instead of
  a bottom band.
- Self-narrow to a sidebar width on startup (`HERDR_FILELIST_WIDTH`, default
  `0.3`; `0` disables) so the pane reads as a sidebar rather than a 50/50 split.

## 0.0.1

- Baseline release. Filelist row that follows the focused pane's cwd, locally
  and over SSH. Recovers the remote cwd from the focused pane's shell prompt
  because herdr drops remote-host OSC 7 and a separate ssh connection always
  starts at the remote home dir. Colorized, git-status aware (local).

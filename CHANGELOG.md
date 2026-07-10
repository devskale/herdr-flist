# Changelog

All notable changes to herdr-flist. Versions follow the manifest `version` field
in `herdr-plugin.toml`.

## 0.4.1

Hardening pass (no behavior change for users):
- tests: `test_filelist.py` (24 unit tests on the pure helpers).
- bound the preview cache (bounded LRU) and prune the remote cache.
- `read_text_file` caps bytes (not just lines) and marks truncation.
- extracted the input layer from `main()` into `handle_click` / `handle_arrow` /
  `handle_enter`; the poll loop is now a short read→dispatch.
- dropped the redundant `ls --color=never` (piped `ls` never colorizes).
- cached symlink→dir resolution; hoisted magic numbers to named constants.
- narrowed broad `except Exception` to specific types; added an env-gated
  `_dbg` log for action failures.

## 0.4.0

- **Settings:** a `\u2699` in the footer opens an in-pane settings overlay with
  live toggles — Show hidden files, Git status, Dirs first, and **Last edited
  first** (sort by mtime, newest first via `ls -t`). Click a row to toggle
  (the overlay stays open), click away to close.
- **Enter** on a selected entry opens it: a directory descends (same as \u2192),
  a file opens with the system default app (`open` on macOS, `xdg-open` on
  Linux).

## 0.3.0

- **Interactive filelist** — the pane is now navigable, not just a display:
  - Click an entry to select it (reverse-video bar); click the selected entry
    again to open a `...` action menu (**Open in new pane**, **cd into followed
    pane** for dirs, **Copy path**).
  - Keyboard (ranger-style, while the pane is focused): focusing auto-selects
    the first entry; **↑/↓** move the selection; **→** descends into a directory
    or opens an **in-pane preview** of a file (filename as the title rule, file
    content beneath; ↑/↓ scroll, ← or click goes back; binary files flagged);
    **←** ascends to the parent directory. Browsing is local-only and resumes
    following the focused pane when you focus another pane. Plain arrows are
    forwarded by herdr only while the pane is focused.
  - The pane enables SGR mouse reporting (`?1000h ?1006h`) and switches its PTY
    to non-canonical mode — a click carries no newline, so cooked mode would
    buffer it forever; this makes clicks and keypresses arrive per-read.
- Symlinked directories (`ln -s` to a dir) are recognized as enterable — `ls -F`
  marks them `@`, not `/`, so they're now resolved and `→` descends into them.

- **Update gating:** the listing only refreshes while its pane is on screen
  (it shares the focused pane's tab). Background tabs pause the polling work —
  no `process-info` / `ls` / `git` / `ssh` calls — and refresh immediately when
  you return to the tab.
- **Click-stable:** focusing the filelist pane itself no longer makes the view
  jump to the plugin root. Self-focus is now detected by pane-id equality
  (`focused.pane_id == HERDR_PANE_ID`) instead of matching the script name in
  the foreground argv, which was unreliable and could miss at click time.
- **Visual overhaul:** the view is laid out to the pane's own size (read from
  its PTY, no herdr call): a title rule with a `~`-abbreviated, left-truncated
  path; a dirs-first listing (now actually sorted, not raw `ls` order) with a
  2-cell git status gutter (`M A D R ? !`); width-aware ellipsization that
  preserves type indicators; and a footer with the item count and git
  branch/ahead-behind. Modified files now show a marker — the old renderer
  silently dropped them.

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

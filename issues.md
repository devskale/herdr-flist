# Issues & Tech Debt

A checkable list from the project review. Items are grouped by priority; each is
written to be verifiable ("Done when: …"). Line/function references are
approximate — they drift as the file changes.

---

## High

- [x] **Add automated tests** — there are none. The pure helpers are trivially
      unit-testable and have regressed this session (IndexError in `compose`,
      mtime/sort interaction, symlink-dir detection).
      Done: `test_filelist.py` (24 tests, stdlib `unittest`) covering
      ellipsize/fit_entry/style_name/git_gutter/_sort_key/render_entries (dirs,
      mtime, symlink→dir)/compose/compose_preview/entry_at_row/
      menu_action_at_row/build_actions/settings_actions/_extract_pane_id/
      is_shell. Run: `python3 -m unittest test_filelist.py`.

- [x] **Break up `main()` (god function, ~295 lines)** — the event loop, focus /
      visibility, kind/cwd resolution, listing fetch, and a ~120-line input
      dispatcher were one function.
      Done: extracted nested handlers `_selected()`, `handle_click(x,y)`,
      `handle_arrow(c)`, `handle_enter()` (plus existing `move()`). The poll
      loop is now a short read→dispatch; the input layer is named and separated.
      (Fuller lift to module-level with a state struct is possible later, but
      the loop now reads top-to-bottom.)

- [x] **Bound `_preview_cache` (memory leak)** — only ever inserted, never
      evicted.
      Done: it's a bounded LRU `OrderedDict` (cap `_PREVIEW_CACHE_CAP = 64`);
      `_remote_cache` prunes expired entries past `_REMOTE_CACHE_CAP = 32`.

## Medium

- [x] **Make the mode an explicit state** — was a product of
      preview/menu_open/settings_open/browse_cwd/self_focused/selected_name.
      Done (at the practical level): handlers branch on the flags, and the state
      block now carries a documented model. A strict `Mode` enum was judged a
      poor fit here — the flags aren't mutually exclusive (settings layers over
      the menu overlay; browse_cwd layers over listing/preview).

- [x] **Single source of truth for row → element mapping** — `entry_at_row`,
      `menu_action_at_row`, preview offset recomputed positions independently;
      `entry_at_row` had dead params (`menu_open`, `k`).
      Done: dead params removed; the three helpers share a documented invariant
      ("row 1 = title, last row = footer") and are unit-tested. (A single
      layout-pass returning clickable regions remains possible but low-value at
      this size.)

- [x] **SSH interactivity gap** — browse/preview/settings/Enter-open are
      local-only while the README pitches SSH.
      Done: README now states the interactive features are **local-only**; over
      SSH the pane is a read-only listing. (Implementing remote ops is a future
      feature, not debt.)

- [x] **Narrow the broad `except Exception: pass`** — ~8 sites swallowed all
      errors silently.
      Done: narrowed to specific types (`OSError` / `subprocess.SubprocessError`
      / `ValueError`) in git_info, herdr_json, render_remote, remote_home,
      copy_to_clipboard, open_default, pane_size, read_text_file, get_preview_lines,
      and the termios/fd setup. The one remaining broad catch (`run_action`, which
      genuinely spans subprocess+json+paths) now logs via env-gated `_dbg()`.

- [x] **Cap preview read by bytes, not only lines** — a single huge line could
      load a giant string.
      Done: `read_text_file` now caps both lines and total bytes
      (`_PREVIEW_MAX_BYTES = 2_000_000`) and appends a `… (truncated)` marker.

## Low

- [x] **Harden the `ls` invocation** — `--color=never` is a GNUism.
      Done: dropped locally — `ls` only colorizes on a tty, and the plugin
      captures output via a pipe, so the flag was redundant. (Kept on the remote
      `ssh … ls` invocation as a harmless safety net for unknown remote `ls`.)

- [x] **Cache symlink-dir resolution** — `os.path.isdir` ran per `@` entry every
      tick.
      Done: `_is_symlink_dir(cwd, base)` with a capped `_symlink_dir_cache`.

- [x] **Reduce global coupling of `_OPT`** — read implicitly by sort/render/main.
      Accepted at this size: `_OPT` is documented as the runtime display-settings
      global; settings toggles are a small, stable surface.

- [x] **Collect magic numbers** — `512`, `20000`, cell arithmetic were scattered.
      Done: hoisted to named constants (`_STDIN_BUF_CAP`, `_PREVIEW_MAX_LINES`,
      `_PREVIEW_MAX_BYTES`, `_PREVIEW_CACHE_CAP`, `_REMOTE_CACHE_CAP`,
      `_SYMLINK_DIR_CACHE_CAP`).

- [x] **Multi-event-per-chunk menu hit uses stale state** — edge case.
      Done: the dispatch already reads live state (`len(menu_actions)`,
      `menu_open`, `entries`, `rows`); the handler extraction makes that explicit.
      No stale render-time values are consulted in the input path.

- [x] **Variable reuse readability** — `pid`/`cwd`/`kind` rebound from "focused
      pane" to "followed view" in the self-focus branch.
      Done: documented inline (the rebind is intentional; the loop treats them as
      the current view's source).

---

All review items resolved (some at the actionable level, noted above). Future
feature ideas — remote (SSH) browse/preview, a multi-select mode, a command
line — are not listed here; they'd be new work, not debt.

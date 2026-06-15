# Building Herdr Extensions — a field guide

A practical, verified walkthrough of the Herdr plugin system, built around a real
plugin: a **filelist row** that follows the focused pane's working directory,
locally *and* over SSH. This guide ships alongside the plugin in this repository.

Everything here was learned by reading the Herdr 0.7.0 source and exercising the
running session, then written down. When Herdr changes, re-check the cited files.

---

## 1. The mental model

A Herdr **plugin** is just a directory with a `herdr-plugin.toml` manifest plus
whatever scripts/binaries the manifest points at. Herdr owns the host surface —
install, manifest validation, panes, events, invocation context, socket access.
The plugin owns its language, dependencies, files, and durable state.

> "A plugin is not an SDK integration. It is a directory with a
> `herdr-plugin.toml` manifest and commands Herdr can launch." — `docs/.../plugins.mdx`

There is **no plugin SDK and no restricted API**. The entire Herdr CLI is the
plugin API. A plugin command is an argv that Herdr spawns; inside it you call
back into Herdr through `$HERDR_BIN_PATH` (or the raw socket). Most plugins are a
shell script, a Node script, or a small binary.

Four extension points, all declared in the manifest:

| Surface      | What it is                                                       |
|--------------|------------------------------------------------------------------|
| `[[actions]]`| A runnable command, invokable from CLI or a keybinding.          |
| `[[events]]` | A command run when a Herdr event fires (`on = "pane.focused"`…). |
| `[[panes]]`  | A command launched inside a real Herdr pane.                     |
| `[[link_handlers]]` | Route modified-clicks on matching URLs to an action.      |

This guide focuses on **panes**, because a "filelist row" is a pane.

---

## 2. The manifest contract

Source of truth: `herdr/src/app/api/plugins/manifest.rs`.

```toml
id = "herdr-flist"                # required. [a-z0-9 : . _ -], <= 120 chars
name = "Filelist"                 # required, non-empty
version = "0.1.0"                 # required, non-empty
min_herdr_version = "0.7.0"       # required, semver, must be <= running herdr
description = "..."               # optional
platforms = ["linux", "macos"]    # recommended; omitting only warns

[[panes]]
id = "row"                        # local id. [a-z0-9 : _ -] — NO dots. unique.
title = "Files"                   # becomes the pane label
placement = "split"               # overlay | split | tab | zoomed (default overlay)
command = ["python3", "filelist.py"] # argv, NOT run through a shell
```

Exact validation rules (verified in `manifest.rs::normalize_*`):

- **Plugin id** (`normalize_identifier`): ASCII letters/digits plus `: . _ -`.
  Dots are allowed, so namespaced ids like `myorg.myplugin` work.
- **Local ids** (action/pane/link-handler — `normalize_local_identifier`): ASCII
  letters/digits plus `: _ -`. **Dots are NOT allowed.** Each must be unique
  within its kind inside the plugin. Herdr qualifies globals as
  `plugin.id.local-id` when needed.
- **`min_herdr_version`** is parsed as semver and compared against the running
  binary. If the plugin requires something newer, link/install fails with
  `plugin_requires_newer_herdr`. Set it to the oldest Herdr that has the APIs
  you use.
- **`platforms`** can be top-level and/or per-item; item-level overrides
  top-level. A local plugin with no top-level `platforms` links with a warning.
- Unknown event names in `[[events]]` don't fail the link — they produce a
  warning instead (see `validate_event_names`).

`command` values are **argv arrays**. Herdr does not shell-expand them. To use
shell features, invoke a shell explicitly (`["sh", "-c", "..."]`) — that's what
the filelist `open` action does.

---

## 3. Plugin panes, and the "row" question

### Placements

```rust
// herdr/src/api/schema/plugins.rs
pub enum PluginPanePlacement { Overlay, Split, Tab, Zoomed }
```

| Placement | Behavior                                                                                  |
|-----------|-------------------------------------------------------------------------------------------|
| `overlay` | Temporary zoomed overlay over the active pane; restores focus/zoom on close. **Default.**|
| `split`   | A real split of the active pane. Direction comes from the *open request*, not manifest.  |
| `tab`     | Opens in a brand-new tab.                                                                 |
| `zoomed`  | Like split but immediately zoomed to fullscreen; forces focus.                            |

**There is no dedicated thin "row" placement.** A "row" is a **`split` opened
with `--direction down`** (a vertical split puts the new pane *below* = a
horizontal row). It is a genuine 50/50-ish split, not a fixed-height strip;
users resize it like any pane (`herdr pane resize`). A true fixed-height "bar"
placement would be a core Herdr change (see §10).

### Opening a pane

`placement` in the manifest is the *default*; the open request overrides it:

```bash
herdr plugin pane open \
  --plugin herdr-flist \
  --entrypoint row \
  --placement split \
  --direction down \      # right | down. down == a horizontal row
  --no-focus              # keep focus on the current pane (default is --focus)
```

Verified in `src/app/api/plugins/panes.rs::open_plugin_split_pane`: when
`direction` is omitted it defaults to `Right`; `Down` → `Direction::Vertical`.
The new pane's estimated size is `rows.max(4)` / `cols.max(10)` and then normal
split layout applies. `--no-focus` (or `placement != zoomed`) leaves focus alone.

### Plugin panes are normal panes

Once open, a plugin pane is an ordinary Herdr pane. It owns a real PTY, shows up
in `herdr pane list`, can be moved/resized/zoomed/closed like any pane, and its
process keeps running until closed. Herdr just remembers it belongs to the plugin
(`PluginPaneRecord { plugin_id, entrypoint }` in `src/app/state.rs`) so ownership
survives moves across tabs/workspaces.

---

## 4. The runtime environment of a plugin command

Verified by reading `panes.rs::plugin_pane_launch_env` + `env.rs::plugin_path_env`,
then confirming against the live plugin.

A pane command is launched with this injected environment:

| Var                          | Meaning                                                        |
|------------------------------|----------------------------------------------------------------|
| `HERDR_BIN_PATH`             | Path to the running Herdr binary. **Call back through this.**  |
| `HERDR_SOCKET_PATH`          | The local socket (Unix socket on Unix, named pipe on Windows). |
| `HERDR_ENV`                  | `1`. Marks "inside herdr".                                     |
| `HERDR_PLUGIN_ID`            | e.g. `herdr-flist`.                                      |
| `HERDR_PLUGIN_ENTRYPOINT_ID` | The pane id from the manifest, e.g. `row`.                     |
| `HERDR_PLUGIN_ROOT`          | Installed/linked plugin directory. **Managed for GitHub plugins — don't store state here.** |
| `HERDR_PLUGIN_CONFIG_DIR`    | Per-plugin user config dir (`~/.config/herdr/plugins/config/<id>`). |
| `HERDR_PLUGIN_STATE_DIR`     | Per-plugin runtime state dir.                                  |
| `HERDR_PLUGIN_CONTEXT_JSON`  | Snapshot of the invocation context (see below).                |

The working directory of a pane command defaults to **`HERDR_PLUGIN_ROOT`**, not
the focused pane's cwd (see `plugin_pane_cwd`). Override with `--cwd` on open.

### `HERDR_PLUGIN_CONTEXT_JSON` — a snapshot, not a stream

This is a single JSON object captured **at open time** (`merge_plugin_context` →
`plugin_context_from_parts` in `src/app/api/plugins/context.rs`):

```json
{
  "workspace_id": "...", "workspace_label": "...", "workspace_cwd": "...",
  "tab_id": "...", "tab_label": "...",
  "focused_pane_id": "...", "focused_pane_cwd": "...",
  "focused_pane_agent": "...", "focused_pane_status": "working",
  "worktree": { ... }, "selected_text": "...",
  "invocation_source": "api", "correlation_id": "..."
}
```

It is **not updated after launch.** If your pane must track live state (a
filelist that follows the focused pane as it moves around), you must **poll**.
That's the single most important architectural fact for this plugin.

---

## 5. Where a pane's cwd actually comes from

This is the heart of "follow the focused pane's cwd".

### OSC 7 is the source of truth

Shells report their cwd by emitting OSC 7: `ESC ] 7 ; file://<host>/<path> BEL`.
Herdr captures this passively (`src/pane/osc.rs::CwdOscTracker`) and stores the
latest value per pane (`PaneRuntime::reported_cwd`). `pane list` exposes it as
each pane's `cwd`. Confirmed live:

```
$ herdr pane list
{..., "panes":[
  {"pane_id":"...:p1","cwd":"/home/pi/code/herdr","focused":false,...},
  {"pane_id":"...:p2","cwd":"/home/pi/code/kontext.one","focused":true,...}
]}
```

So **the robust, supported way to read a pane's cwd from a plugin is
`herdr pane list`** (or `pane get`) and read the `cwd` field. Fallbacks:
- If OSC 7 hasn't fired yet, `cwd` may be empty. `pane process-info --pane <id>`
  returns OS-level `cwd` per foreground process (read from `/proc`), which you
  can use as a fallback for local panes.
- For agent/working panes, `foreground_cwd` is also present.

### The local-vs-SSH problem (and why OSC 7 matters here)

Here's the catch, in `src/pane/osc.rs::parse_file_uri_cwd`:

```rust
let host = &rest[..slash];
if !(host.is_empty() || host.eq_ignore_ascii_case("localhost")) {
    return None;   // <-- remote hosts are DROPPED
}
```

When a pane is SSH'd into a remote box, the **remote** shell emits
`file://remotehost/remote/path`. Herdr sees a non-local host and **discards it**,
so the SSH pane's `cwd` is `None` (or stale from before the ssh). That means:

- A naive filelist reading `pane list` `cwd` sees nothing for SSH panes.
- The remote path is genuinely remote; even if Herdr kept the host, the *local*
  plugin can't `ls` it — it must run `ssh remotehost ls`.

**The workaround used by this plugin (no core change needed):** detect SSH from
the pane's foreground process, then recover the remote cwd from the focused
pane's own shell prompt, and list *that exact path* over ssh.

1. `herdr pane process-info --pane <focused>` -> `foreground_processes[].argv`.
   An SSH pane's foreground process is `["ssh", ..., "user@host", ...]`.
2. Parse the destination token out of the ssh argv (skip options + their values;
   the first remaining bare token is the destination). Works for `ssh user@host`,
   `ssh host`, `mosh host`, `ssh -p 2222 user@host`, etc.
3. **Recover the remote cwd from the focused pane's prompt.** Read the pane's
   recent scrollback (`pane read --source recent-unwrapped`) and match the last
   shell prompt. Common shapes:
   - `user@host:~/path$` / `user@host:/path#` (by far the most common)
   - bare `~/path$`, `/path>`, `/path%`
   Strip ANSI color codes first. Take the last non-empty line so an in-flight
   command line doesn't shadow the real prompt.
4. Run `ssh -o BatchMode=yes -o ConnectTimeout=3 <dest> 'ls -FA1 -- <path>'` to
   list *that path* on the remote. If no prompt could be parsed yet, fall back
   to listing the remote login dir.

`BatchMode=yes` is essential — it makes ssh fail fast instead of hanging on a
password prompt inside an unattended pane. The plugin caches each remote
`(dest, path)` listing for a few seconds to avoid hammering.

**Why this works into subdirectories:** a *separate* ssh connection has no idea
where the interactive session has `cd`'d to — it always starts at the home dir.
The prompt is the only reliable window into the interactive session's current
remote cwd, because the prompt is rendered *by that session's own shell*. Parsing
it lets the filelist follow `cd` on the remote. See `filelist.py` ->
`remote_cwd_from_pane` and `render_remote`.

### There is no `cwd_changed` event

`src/api/schema/events.rs` defines the hookable events. Notably:

```
workspace.created/updated/closed/renamed/focused
worktree.created/opened/removed
tab.created/closed/renamed/focused
pane.created/closed/focused/moved/exited
pane.agent_detected / pane.agent_status_changed
```

There is **`pane.focused`** (fires on focus change) but **no `pane.cwd_changed`**
and **no `pane.output_changed`** for hooks (it's deliberately excluded from the
hookable list — see the test `plugin_hook_event_names_exclude_unemitted_output_change`).
So a pane that must reflect cwd changes *within the same focused pane* has to
**poll**. The filelist polls `pane list` once per second.

---

## 6. The filelist plugin, annotated

```
herdr-flist/
  herdr-plugin.toml   # manifest (one pane "row", one action "open")
  filelist.py         # the pane command: a long-running redraw loop
  README.md
  EXTENDING.md        # this guide
  LICENSE             # MIT
```

### What `filelist.py` does, per tick (1 s)

1. `focused_pane()` — `herdr pane list` -> JSON -> the focused pane's id + record.
2. `fg_info()` — `herdr pane process-info --pane <id>` -> foreground
   `name`, joined `argv`, and OS `cwd` (fallback for local).
3. **Self-detection:** if the focused pane's argv contains the script's own
   basename (`filelist.py`), the filelist pane *itself* is focused — keep showing
   the last real pane instead of the plugin root.
4. **SSH detection:** if the foreground is `ssh`/`mosh`, parse the destination,
   recover the remote cwd from the focused pane's prompt (§5), and render over
   ssh (cached).
5. Otherwise **local**: prefer `pane list` `cwd` (OSC 7), fall back to the
   process `cwd`; `ls -FA1`, classify by trailing indicator, and tag git status
   from `git status --porcelain`.
6. **Redraw only on change** (`render != last_render`) using cursor-home +
   clear-to-end, so there's no flicker and ssh isn't spammed.

### Why a long-running process?

A plugin pane is a real terminal. The simplest "list once and exit" pane would
flash and die. For something that *follows* live state, you want a process that
owns the pane and redraws. The trade-off is that it occupies a pane and makes
herdr calls every tick — keep the interval modest (1 s) and cache remote calls.

---

## 7. Build & test loop (the exact commands we used)

```bash
# 1. Link the local plugin into the running herdr server
herdr plugin link ./herdr-flist

# 2. Inspect it (human-readable)
herdr plugin list
herdr plugin action list --plugin herdr-flist

# 3. Open the filelist as a row below the focused pane, without stealing focus
herdr plugin pane open \
  --plugin herdr-flist --entrypoint row \
  --placement split --direction down --no-focus

# 4. Read what it rendered
herdr pane read <filelist-pane-id> --source recent --lines 40

# 5. Prove focus-following: switch focus, re-read
herdr pane focus --pane <other-pane-id>

# 6. Watch plugin command logs (actions/events — pane output isn't logged here)
herdr plugin log list --plugin herdr-flist

# Tear down
herdr plugin pane close <filelist-pane-id>
herdr plugin unlink herdr-flist          # unregister, keep files
```

**Caveat when testing from inside herdr:** the herdr CLI talks to whichever
server your env points at. To exercise a freshly *built* debug binary against the
debug server (not the installed stable one), scrub the inherited socket env:

```bash
env -u HERDR_SOCKET_PATH -u HERDR_CLIENT_SOCKET_PATH cargo run -- plugin link ./herdr-flist
```

For this plugin we didn't need core changes, so linking into the installed
`herdr` was enough.

### Distribution

Local: `herdr plugin link <dir>`. Shareable: push to GitHub with
`herdr-plugin.toml` at the root (or under a subdir), then anyone can
`herdr plugin install owner/repo[/subdir]`. `install` clones, shows a preview in
an interactive terminal, runs declared `[[build]]` commands, and registers under
Herdr-managed plugin data. `link` skips builds (you build your working tree).

---

## 8. Wiring a keybinding and reacting to events

Bind the plugin's action to a key (`~/.config/herdr/config.toml`):

```toml
[[keys.command]]
key = "prefix+f"
type = "plugin_action"
command = "herdr-flist.open"      # plugin.id.action-id
description = "open filelist row"
```

Because the action's command itself calls
`$HERDR_BIN_PATH plugin pane open ...`, the keybinding opens the row in one step.

Event hooks are declared, not registered at runtime:

```toml
[[events]]
on = "pane.focused"                     # see §5 for the full event list
command = ["sh", "-c", "echo focused >> \"$HERDR_PLUGIN_STATE_DIR\"/events.log"]
```

The hook command receives `HERDR_PLUGIN_EVENT` and `HERDR_PLUGIN_EVENT_JSON`.
Remember: there is no `pane.cwd_changed`, so events can't drive a live filelist
on their own — pair them with polling if you want both snappy focus reactions and
intra-pane cwd updates.

---

## 9. Storage

There is **no Herdr-managed plugin storage API in v1.** Use the dirs Herdr
creates for you:

- `HERDR_PLUGIN_CONFIG_DIR` — user-editable config (`.env`, settings). Seeded
  from legacy locations when present; Herdr never validates/syncs/deletes it.
- `HERDR_PLUGIN_STATE_DIR` — local runtime state, caches, logs, sqlite, etc.

Never write durable state under `HERDR_PLUGIN_ROOT` for a GitHub-installed
plugin — that directory is a managed source checkout and can be replaced on
reinstall.

---

## 10. Limitations, gotchas, and where the core would have to change

1. **No fixed-height "row"/"bar" placement.** A row today is a resizable split.
   A true status-bar-style placement (fixed height, non-focusable, docked) is a
   core addition: a new `PluginPanePlacement` variant plus layout support in
   `src/workspace/`/`src/layout.rs`.

2. **SSH cwd is dropped by `parse_file_uri_cwd`.** The clean core fix is to
   preserve the remote host (e.g. expose `cwd` as `host:path`, or a separate
   `remote_cwd`/`remote_host` on `PaneInfo`). Until then, plugins detect ssh via
   `pane process-info` and shell out themselves (this plugin's approach).

3. **No `pane.cwd_changed` event.** Live-following requires polling `pane list`.
   Adding cwd-change emission (on `reported_cwd` update in `PaneRuntime`) would
   let filelist-style plugins be event-driven and cheaper.

4. **Context JSON is a launch snapshot.** Don't rely on it for live values; poll
   or use events.

5. **Plugin pane cwd defaults to the plugin root**, not the focused pane. Pass
   `--cwd` if you want otherwise, or ignore it and read cwd from the API like we
   do.

6. **Platform coverage.** The manifest can declare `windows`, but `filelist.py`
   is Python; a Windows port would be a PowerShell pane command. Pane commands use
   Herdr's normal Windows launcher and must be valid Windows argv.

7. **Self-focus feedback.** When the user focuses the plugin pane itself, naive
   "follow focused" logic chases its own cwd. Detect self (e.g. by the script
   basename in the foreground argv) and pin to the last real pane.

8. **Don't `unwrap()` / don't add deps casually / platform code stays isolated**
   — these are Herdr's own conventions (`AGENTS.md`) and apply if you ever patch
   the core to address items 1–3.

---

## 11. File map for hacking on this feature

| If you want to… | Read |
|-----------------|------|
| Change manifest validation / add a field | `herdr/src/app/api/plugins/manifest.rs` |
| Add a placement or change pane opening | `herdr/src/api/schema/plugins.rs`, `herdr/src/app/api/plugins/panes.rs` |
| Change what context is injected | `herdr/src/app/api/plugins/context.rs`, `env.rs` |
| Change cwd reporting (OSC 7) | `herdr/src/pane/osc.rs`, `herdr/src/pane.rs` (`reported_cwd`) |
| Add/change hookable events | `herdr/src/api/schema/events.rs`, `runtime.rs` |
| Plugin install/link CLI | `herdr/src/cli/plugin.rs` |
| Docs (unreleased) | `herdr/docs/next/website/src/content/docs/plugins.mdx` |

---

*Built and verified against Herdr 0.7.0. herdr-flist is a community plugin,
not part of Herdr itself.*

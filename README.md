# herdr-flist

Python Herdr plugin that opens a filelist row under the focused pane and follows
its working directory — locally and over SSH.

## Setup

Requires Herdr 0.7.0 or newer and `python3` (stdlib only). SSH panes need
passwordless key auth to the remote host (`ssh-copy-id`), since the remote
listing runs non-interactively with `BatchMode=yes`.

Install from GitHub:

```sh
herdr plugin install devskale/herdr-flist
```

Or clone and link locally while developing:

```sh
git clone https://github.com/devskale/herdr-flist.git
cd herdr-flist
herdr plugin link .
```

## Behavior

Open the row under the focused pane without stealing focus:

```sh
herdr plugin action invoke herdr-flist.open
# or directly:
herdr plugin pane open --plugin herdr-flist --entrypoint row \
  --placement split --direction down --no-focus
```

Bind it to a key in `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+f"
type = "plugin_action"
command = "herdr-flist.open"
description = "open filelist row"
```

The row shows a live listing of the focused pane's cwd:

- Local panes: the cwd comes from Herdr (OSC 7). Entries are dirs-first,
  colorized by type, and tagged with git status (`+new` `-del` `~ren`
  `?untracked` `!conflict`).
- SSH panes: Herdr drops remote-host cwd reporting, so the remote cwd is parsed
  from the focused pane's own shell prompt and listed over `ssh`. This is what
  lets the view follow the remote shell into subdirectories.

It redraws only when the view changes and caches remote listings for a few
seconds.

## Notes

- Single file, no pip dependencies: `filelist.py`.
- Detects SSH from the foreground process (`ssh`/`mosh`) and parses the
  destination out of the argv, skipping options that take a value (`-p`, `-i`…).
- Recognizes common prompt shapes: `user@host:~/path$`, bare `~/path$`, `/path>`.
- Holds the last real view when the filelist pane itself is focused.
- Tunables (environment variables): `HERDR_FILELIST_INTERVAL` (default `1`s),
  `HERDR_FILELIST_REMOTE_CACHE` (`3`s), `HERDR_FILELIST_SSH_TIMEOUT` (`5`s).

See `EXTENDING.md` for a field guide to the Herdr plugin model built around this
plugin — manifest contract, injected environment, where pane cwd comes from, and
the SSH cwd gap that motivated the prompt-parsing approach.

herdr-flist is a community plugin, not part of Herdr itself.

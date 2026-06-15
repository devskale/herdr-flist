#!/usr/bin/env python3
"""filelist.py — herdr plugin pane command (entrypoint id "row").

A live file listing that follows the focused pane's working directory.

  - local pane : cwd comes from herdr (OSC 7 via `pane list`), with a
                 process-cwd fallback.
  - ssh  pane  : the remote cwd is parsed from the focused pane's shell prompt
                 (herdr drops remote OSC 7, and a *separate* ssh connection
                 always starts at the remote home dir, so the prompt is the only
                 reliable window into the interactive session's cwd). We then
                 `ssh dest 'ls <that path>'`.

Long-running: owns its pane, redraws only when the view changes. See
herdr-extension-guide.md.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time

HERDR = os.environ.get("HERDR_BIN_PATH", "herdr")
INTERVAL = float(os.environ.get("HERDR_FILELIST_INTERVAL", "1"))
REMOTE_CACHE = float(os.environ.get("HERDR_FILELIST_REMOTE_CACHE", "3"))
SSH_TIMEOUT = float(os.environ.get("HERDR_FILELIST_SSH_TIMEOUT", "5"))
SELF_TOKEN = os.path.basename(__file__)  # for "this pane is me" detection

# --- ANSI -----------------------------------------------------------------
R = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
BLUE = "\033[34m"
GREEN = "\033[32m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RED = "\033[31m"
MAGENTA = "\033[35m"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_ANSI_ALL_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Prompt shapes we try to pull a cwd out of. The path must start with `/` or
# `~` (i.e. `\w`-style), which keeps false positives low.
PROMPT_PATTERNS = [
    # user@host:~/path$  /  user@host:/path#   (the overwhelmingly common form)
    re.compile(r"\S+@\S+:([~/][^\$#]*?)\s*[$#]\s*$"),
    # bare ~/path$  /  /path>  /  /path%       (custom / minimal prompts)
    re.compile(r"([~/][^\$#>%]*?)\s*[$#>%]\s*$"),
]

SSH_RE = re.compile(r"(^|\s)(ssh|mosh)(\s|$)")
SSH_VALUE_OPTS = set("ilopFEJLRDWwbcm".split())

# --- herdr CLI helpers -----------------------------------------------------
def herdr_json(args: list[str]):
    try:
        out = subprocess.run(
            [HERDR, *args], capture_output=True, text=True
        ).stdout
        return json.loads(out)
    except Exception:
        return None


def focused_pane():
    d = herdr_json(["pane", "list"])
    if not isinstance(d, dict):
        return None, None
    for p in d.get("result", {}).get("panes", []):
        if p.get("focused"):
            return p.get("pane_id"), p
    return None, None


def fg_info(pane_id):
    """Return (name, argv_str, os_cwd) for the pane's foreground process."""
    if not pane_id:
        return "", "", None
    d = herdr_json(["pane", "process-info", "--pane", pane_id])
    procs = (
        d.get("result", {}).get("process_info", {}).get("foreground_processes")
        if isinstance(d, dict)
        else None
    ) or []
    if not procs:
        return "", "", None
    p = procs[0]
    argv = p.get("argv") or (p.get("cmdline", "").split() if p.get("cmdline") else [])
    return p.get("name", ""), " ".join(argv), p.get("cwd")


def pane_text(pane_id, lines=80):
    if not pane_id:
        return ""
    return subprocess.run(
        [HERDR, "pane", "read", pane_id, "--source", "recent-unwrapped",
         "--lines", str(lines)],
        capture_output=True, text=True,
    ).stdout


# --- ssh destination parsing ----------------------------------------------
def ssh_dest(argv_str: str):
    """Pull the destination token out of an `ssh ... user@host ...` cmdline."""
    toks = argv_str.split()
    i = 1 if toks and toks[0] in ("ssh", "mosh") else 0
    dest = None
    while i < len(toks):
        t = toks[i]
        if t == "--":
            if i + 1 < len(toks):
                dest = toks[i + 1]
            break
        if t.startswith("-") and not t.startswith("---"):
            # skip the argument of options that take a value (-p 22, -i key, ...)
            if len(t) == 2 and t[1] in SSH_VALUE_OPTS:
                i += 2
                continue
            i += 1
            continue
        dest = t  # first bare token is the destination
        break
    return dest


# --- remote cwd from the focused pane's prompt ----------------------------
def remote_cwd_from_pane(pane_id):
    """Best-effort: parse the cwd out of the last shell prompt in the pane."""
    text = pane_text(pane_id)
    for raw in reversed(text.splitlines()):
        if not raw.strip():
            continue
        line = _ANSI_RE.sub("", raw)
        for pat in PROMPT_PATTERNS:
            m = pat.search(line)
            if m:
                path = m.group(1).strip()
                if path:
                    return path
    return None


_remote_home: dict[str, str | None] = {}
_remote_cache: dict[tuple, tuple] = {}  # (dest, path) -> (listing, expires_at)


def remote_home(dest):
    if dest not in _remote_home:
        try:
            r = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
                 dest, "printf %s \"$HOME\""],
                capture_output=True, text=True, timeout=SSH_TIMEOUT,
            )
            _remote_home[dest] = (r.stdout.strip() or None)
        except Exception:
            _remote_home[dest] = None
    return _remote_home[dest]


def expand_remote_path(dest, path):
    home = remote_home(dest)
    if home:
        if path == "~":
            return home
        if path.startswith("~/"):
            return home + path[1:]
    return path


def render_remote(dest, path):
    """List `path` on `dest` over ssh. If path is None, list the login dir."""
    key = (dest, path)
    now = time.time()
    cached = _remote_cache.get(key)
    if cached and now < cached[1]:
        return cached[0]

    if path:
        full = expand_remote_path(dest, path)
        remote_cmd = "ls -FA1 --color=never -- " + shlex.quote(full)
    else:
        remote_cmd = "pwd; printf '\\x1f'; ls -FA1 --color=never"

    try:
        out = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
             dest, remote_cmd],
            capture_output=True, text=True, timeout=SSH_TIMEOUT,
        ).stdout
    except Exception:
        out = ""
    _remote_cache[key] = (out, now + REMOTE_CACHE)
    return out


# --- local listing --------------------------------------------------------
def git_status_map(cwd):
    try:
        if subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
        ).returncode != 0:
            return {}
        st = subprocess.run(
            ["git", "-C", cwd, "status", "--porcelain=v1"],
            capture_output=True, text=True,
        ).stdout
    except Exception:
        return {}
    m = {}
    for line in st.splitlines():
        if len(line) < 4:
            continue
        code, f = line[:2], line[3:].strip()
        tag = "M"
        if "A" in code:
            tag = "A"
        elif "D" in code:
            tag = "D"
        elif "R" in code:
            tag = "R"
        elif "?" in code:
            tag = "?"
        elif "U" in code:
            tag = "U"
        m[f.split(" -> ")[0]] = tag
    return m


def render_local(cwd):
    if not os.path.isdir(cwd):
        return f"{DIM}(not a directory: {cwd}){R}"
    gmap = git_status_map(cwd)
    try:
        listing = subprocess.run(
            ["ls", "-FA1", "--color=never"], cwd=cwd,
            capture_output=True, text=True,
        ).stdout
    except Exception:
        return f"{DIM}(cannot list {cwd}){R}"

    out = []
    for entry in listing.splitlines():
        if not entry:
            continue
        if entry.endswith("/"):
            tag = f"{BLUE}{BOLD}{entry}{R}"
        elif entry.endswith("|"):
            tag = f"{YELLOW}{entry}{R}"
        elif entry.endswith("="):
            tag = f"{MAGENTA}{entry}{R}"
        elif entry.endswith("@"):
            tag = f"{CYAN}{entry}{R}"
        else:
            full = os.path.join(cwd, entry)
            tag = f"{GREEN}{entry}{R}" if os.access(full, os.X_OK) else entry
        base = entry.rstrip("/@=*|")
        code = gmap.get(base)
        # also flag if a modified entry lives under this directory
        if code is None:
            code = next(
                (v for k, v in gmap.items() if k.startswith(base + "/")), None
            )
        if code == "A":
            tag += f" {GREEN}(+new){R}"
        elif code == "D":
            tag += f" {RED}(-del){R}"
        elif code == "R":
            tag += f" {YELLOW}(~ren){R}"
        elif code == "?":
            tag += f" {DIM}(?untracked){R}"
        elif code == "U":
            tag += f" {RED}(!conflict){R}"
        out.append(tag)
    return "\n".join(out)


def format_remote_listing(body, dest):
    if not body:
        return f"{DIM}(ssh {dest}: no listing){R}"
    rcwd, _, entries = body.partition("\x1f")
    lines = [f"{BOLD}{rcwd}{R} ({dest})"]
    for e in entries.splitlines():
        if not e:
            continue
        if e.endswith("/"):
            lines.append(f"{BLUE}{BOLD}{e}{R}")
        elif e.endswith("@"):
            lines.append(f"{CYAN}{e}{R}")
        elif e.endswith("|"):
            lines.append(f"{YELLOW}{e}{R}")
        else:
            lines.append(e)
    return "\n".join(lines)


# --- main loop ------------------------------------------------------------
def main():
    last = {"pid": None, "cwd": None, "kind": None, "ssh": None}
    last_render = ""

    sys.stdout.write("\033[?25l")  # hide cursor
    sys.stdout.flush()

    def restore():
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()

    try:
        while True:
            pid, pane = focused_pane()
            name, argv, proc_cwd = fg_info(pid)
            osc_cwd = pane.get("cwd") if pane else None

            kind = cwd = ssh_target = None

            if SELF_TOKEN in argv:
                # the filelist pane itself is focused: hold the last real view
                pid = last["pid"]
                cwd = last["cwd"]
                kind = last["kind"]
                ssh_target = last.get("ssh")
            elif SSH_RE.search(name + " " + argv):
                dest = ssh_dest(argv)
                if dest:
                    kind, cwd, ssh_target = "ssh", dest, dest

            if not kind:
                if osc_cwd:
                    kind, cwd = "local", osc_cwd
                elif proc_cwd:
                    kind, cwd = "local", proc_cwd

            header = body = ""
            if kind == "local" and cwd:
                header, body = cwd, render_local(cwd)
            elif kind == "ssh" and ssh_target:
                rpath = remote_cwd_from_pane(pid) if pid else None
                if rpath:
                    header = f"{ssh_target}:{rpath}"
                    body = format_remote_listing(render_remote(ssh_target, rpath), ssh_target)
                else:
                    header = f"ssh {ssh_target} (home)"
                    body = format_remote_listing(render_remote(ssh_target, None), ssh_target)
            else:
                header = "(no cwd reported)"
                body = "focus a pane that has reported a working directory."

            if pid and kind:
                last = {"pid": pid, "cwd": cwd, "kind": kind, "ssh": ssh_target}

            render = f"{BOLD}{header}{R}\n{body}"
            if render != last_render:
                last_render = render
                sys.stdout.write("\033[H\033[2J" + render + "\n")
                sys.stdout.flush()

            time.sleep(INTERVAL)
    except KeyboardInterrupt:
        pass
    finally:
        restore()


if __name__ == "__main__":
    main()

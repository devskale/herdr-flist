#!/usr/bin/env -S uv run
"""filelist.py — herdr plugin pane command (entrypoint id "row").

A live file listing that follows the focused pane's working directory.

  - local pane : cwd comes from herdr (OSC 7 via `pane list`), with a
                 process-cwd fallback.
  - ssh  pane  : the remote cwd is parsed from the focused pane's shell prompt
                 (herdr drops remote OSC 7, and a *separate* ssh connection
                 always starts at the remote home dir, so the prompt is the only
                 reliable window into the interactive session's cwd). We then
                 `ssh dest 'ls <that path>'`.

The view is laid out to the pane's own size (read from its PTY — no herdr
call): a title rule, a dirs-first listing with a git status gutter, and a
footer status line. It only refreshes while on screen and holds its view when
focused itself. Click an entry to select it (reverse-video bar); click the
selected entry again to open a `...` action menu. See PLUGINS.md.
"""
# /// script
# requires-python = ">=3.9"
# dependencies = []
# description = "herdr-flist: a filelist row that follows the focused pane's cwd."
# ///
from __future__ import annotations

import json
import os
import re
import select
import shlex
import shutil
import subprocess
import sys
import termios
import time
from collections import OrderedDict

HERDR = os.environ.get("HERDR_BIN_PATH", "herdr")
INTERVAL = float(os.environ.get("HERDR_FILELIST_INTERVAL", "1"))
REMOTE_CACHE = float(os.environ.get("HERDR_FILELIST_REMOTE_CACHE", "3"))
SSH_TIMEOUT = float(os.environ.get("HERDR_FILELIST_SSH_TIMEOUT", "5"))
# On startup, narrow this pane toward this fraction of its parent region so it
# reads as a sidebar column instead of a 50/50 split. 0 disables self-sizing.
SIDEBAR_FRACTION = float(os.environ.get("HERDR_FILELIST_WIDTH", "0.3"))
# Command used by the "Open in new pane" action on a file (default: $EDITOR, vi).
OPENER = os.environ.get("HERDR_FILELIST_OPENER") or os.environ.get("EDITOR") or "vi"
SELF_TOKEN = os.path.basename(__file__)  # for "this pane is me" detection
ME_ID = os.environ.get("HERDR_PANE_ID", "")  # this pane's own id (visibility gating)

_SHELLS = {"bash", "zsh", "fish", "sh", "dash", "ksh", "tcsh", "csh", "ash", "nu"}

# Runtime display settings (toggled via the footer gear).
_OPT = {"hidden": True, "git": True, "dirs": True, "mtime": False}

# Internal caps (named instead of magic numbers).
_STDIN_BUF_CAP = 512
_PREVIEW_MAX_LINES = 20_000
_PREVIEW_MAX_BYTES = 2_000_000
_PREVIEW_CACHE_CAP = 64
_REMOTE_CACHE_CAP = 32
_SYMLINK_DIR_CACHE_CAP = 256

# --- ANSI -----------------------------------------------------------------
R = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
REV = "\033[7m"
BLUE = "\033[34m"
GREEN = "\033[32m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RED = "\033[31m"
MAGENTA = "\033[35m"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

PROMPT_PATTERNS = [
    re.compile(r"\S+@\S+:([~/][^\$#]*?)\s*[$#]\s*$"),
    re.compile(r"([~/][^\$#>%]*?)\s*[$#>%]\s*$"),
]

SSH_RE = re.compile(r"(^|\s)(ssh|mosh)(\s|$)")
SSH_VALUE_OPTS = set("ilopFEJLRDWwbcm".split())

_INDICATORS = "/@=*|%>"  # trailing `ls -F` type indicators

# SGR mouse event: ESC [< button ; x ; y M(press)/m(release). x,y are 1-based.
MOUSE_RE = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
# Arrow keys, normal (ESC [ A) and application-cursor (ESC O A) forms.
KEY_RE = re.compile(rb"\x1b(?:\[|O)[ABCD]")

# --- herdr CLI helpers -----------------------------------------------------
def _dbg(msg):
    """Env-gated debug log to a file (stderr would corrupt the TUI pane)."""
    if not os.environ.get("HERDR_FILELIST_DEBUG"):
        return
    try:
        with open("/tmp/herdr-flist-debug.log", "a") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def herdr_json(args: list[str]):
    try:
        out = subprocess.run(
            [HERDR, *args], capture_output=True, text=True
        ).stdout
        return json.loads(out)
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def pane_entries():
    """All panes from `pane list` (empty list on failure)."""
    d = herdr_json(["pane", "list"])
    if not isinstance(d, dict):
        return []
    return d.get("result", {}).get("panes", []) or []


def find_focused(panes):
    return next((p for p in panes if p.get("focused")), None)


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


def is_shell(name: str) -> bool:
    return name in _SHELLS


# --- self-sizing into a sidebar column -------------------------------
def narrow_self():
    """Shrink this pane toward SIDEBAR_FRACTION of its parent region."""
    if not (0 < SIDEBAR_FRACTION < 0.5):
        return
    pane_id = os.environ.get("HERDR_PANE_ID", "")
    if not pane_id:
        return
    d = herdr_json(["pane", "layout"])
    layout = (d or {}).get("result", {}).get("layout", {}) if isinstance(d, dict) else {}
    area = layout.get("area", {})
    me = next(
        (p for p in layout.get("panes", []) if p.get("pane_id") == pane_id), None
    )
    if not me or not area.get("width") or not me["rect"].get("width"):
        return
    parent_w = float(area["width"])
    cur_frac = float(me["rect"]["width"]) / parent_w
    if cur_frac <= SIDEBAR_FRACTION + 0.02:
        return
    delta = cur_frac - SIDEBAR_FRACTION
    subprocess.run(
        [HERDR, "pane", "resize", "--pane", pane_id,
         "--direction", "right", "--amount", f"{delta:.4f}"],
        capture_output=True, text=True,
    )


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
        except (subprocess.SubprocessError, OSError):
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
    """Return raw `ls -FA1` entries for `path` on `dest` over ssh."""
    key = (dest, path)
    now = time.time()
    cached = _remote_cache.get(key)
    if cached and now < cached[1]:
        return cached[0]

    if path:
        full = expand_remote_path(dest, path)
        remote_cmd = "ls -FA1 --color=never -- " + shlex.quote(full)
    else:
        remote_cmd = "ls -FA1 --color=never"

    try:
        out = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
             dest, remote_cmd],
            capture_output=True, text=True, timeout=SSH_TIMEOUT,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        out = ""
    _remote_cache[key] = (out, now + REMOTE_CACHE)
    if len(_remote_cache) > _REMOTE_CACHE_CAP:  # prune expired entries
        for stale in [k for k, v in _remote_cache.items() if v[1] <= now]:
            _remote_cache.pop(stale, None)
    return out


# --- terminal size & text helpers ----------------------------------------
def pane_size():
    """This pane's (cols, rows) from its own PTY. No herdr call needed."""
    try:
        sz = shutil.get_terminal_size((80, 24))
        return sz.columns, sz.lines
    except OSError:
        return 80, 24


def ellipsize(s, width):
    if width <= 0:
        return ""
    if len(s) <= width:
        return s
    if width == 1:
        return "…"
    return s[: width - 1] + "…"


def ellipsize_left(s, width):
    if width <= 0:
        return ""
    if len(s) <= width:
        return s
    if width == 1:
        return "…"
    return "…" + s[-(width - 1):]


def shorten_path(path, width):
    home = os.path.expanduser("~")
    if home and path == home:
        s = "~"
    elif home and path.startswith(home + os.sep):
        s = "~" + path[len(home):]
    else:
        s = path
    return ellipsize_left(s, width)


def _split_indicator(entry):
    for c in ("/", "@", "*", "|", "="):
        if entry.endswith(c):
            return entry[:-1], c
    return entry, ""


def fit_entry(entry, maxw):
    """Ellipsize an `ls -F` token, keeping its trailing type indicator."""
    if len(entry) <= maxw:
        return entry
    core, ind = _split_indicator(entry)
    avail = maxw - len(ind)
    if avail <= 1:
        return ellipsize(entry, maxw)
    return ellipsize(core, avail) + ind


# --- coloring -------------------------------------------------------------
def style_name(entry):
    if entry.endswith("/"):
        return f"{BLUE}{BOLD}{entry}{R}"
    if entry.endswith("@"):
        return f"{CYAN}{entry}{R}"
    if entry.endswith("|"):
        return f"{YELLOW}{entry}{R}"
    if entry.endswith("="):
        return f"{MAGENTA}{entry}{R}"
    if entry.endswith("*"):
        return f"{GREEN}{entry[:-1]}{R}"
    return entry


def git_gutter(code):
    if code == "M":
        return f"{YELLOW}M{R} "
    if code == "A":
        return f"{GREEN}A{R} "
    if code == "D":
        return f"{RED}D{R} "
    if code == "R":
        return f"{YELLOW}R{R} "
    if code == "?":
        return f"{DIM}?{R} "
    if code == "U":
        return f"{RED}!{R} "
    return "  "


def git_info(cwd):
    """Return (status_map, branch_label) for cwd; ({}, None) if not a repo."""
    try:
        if subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
        ).returncode != 0:
            return {}, None
        st = subprocess.run(
            ["git", "-C", cwd, "status", "--porcelain=v1", "-b"],
            capture_output=True, text=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return {}, None
    lines = st.splitlines()
    branch = None
    start = 0
    if lines and lines[0].startswith("## "):
        start = 1
        hdr = lines[0][3:]
        ahead = behind = ""
        m = re.search(r"\[([^\]]*)\]", hdr)
        if m:
            inside = m.group(1)
            ah = re.search(r"ahead\s+(\d+)", inside)
            bd = re.search(r"behind\s+(\d+)", inside)
            if ah:
                ahead = f" ↑{ah.group(1)}"
            if bd:
                behind = f" ↓{bd.group(1)}"
        if "no branch" in hdr.lower():
            name = "DETACHED"
        elif hdr.startswith("No commits yet on ") or hdr.startswith("Initial commit on "):
            name = hdr.split(" on ")[-1].strip()
        else:
            name = hdr.split("...")[0].split(" ")[0]
        if name:
            branch = f"{name}{ahead}{behind}"
    gmap = {}
    for line in lines[start:]:
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
        gmap[f.split(" -> ")[0]] = tag
    return gmap, branch


# --- entry rendering ------------------------------------------------------
def _sort_key(entry):
    name = entry.rstrip(_INDICATORS).lower()
    if _OPT.get("dirs", True):
        return (0 if entry.endswith("/") else 1, name)
    return (name,)


# (cwd, name) -> is_dir, capped; symlink targets rarely flip, so we don't bust
# per-tick — the cap keeps memory bounded.
_symlink_dir_cache: dict[tuple[str, str], bool] = {}


def _is_symlink_dir(cwd, base):
    """True if `base` (a symlink, local) resolves to a directory. Cached."""
    if not cwd:
        return False
    key = (cwd, base)
    hit = _symlink_dir_cache.get(key)
    if hit is None:
        try:
            hit = os.path.isdir(os.path.join(cwd, base))
        except OSError:
            hit = False
        _symlink_dir_cache[key] = hit
        if len(_symlink_dir_cache) > _SYMLINK_DIR_CACHE_CAP:
            _symlink_dir_cache.pop(next(iter(_symlink_dir_cache)))
    return hit


def render_entries(raw, gmap, width, cwd=None):
    """Turn raw `ls -FA1` output into dirs-first entry records.

    A symlink (`@`) that resolves to a directory (local `cwd` only) is treated
    as a directory so → can descend into it; `ls -F` marks such entries with
    `@`, not `/`.
    """
    max_name = max(1, width - 2)
    items = [e for e in raw.splitlines() if e]
    if not _OPT.get("mtime"):  # mtime order arrives pre-sorted from `ls -t`
        items.sort(key=_sort_key)
    out = []
    for entry in items:
        base = entry.rstrip(_INDICATORS)
        code = None
        if gmap:
            code = gmap.get(base)
            if code is None:
                code = next(
                    (v for k, v in gmap.items() if k.startswith(base + "/")), None
                )
        token = fit_entry(entry, max_name)
        is_dir = entry.endswith("/") or _is_symlink_dir(cwd, base)
        out.append({
            "line": git_gutter(code) + style_name(token),
            "token": token,
            "name": base,
            "dir": is_dir,
        })
    return out


def _msg(text):
    return [{"line": f"{DIM}{text}{R}", "token": text, "name": "", "dir": False}]


def _bar(token, cols, hint=True):
    """Full-width reverse-video selection bar. `hint` shows a `...` affordance."""
    if hint:
        tok = fit_entry(token, max(1, cols - 5))
        body = "  " + tok
        pad = max(0, cols - len(body) - 3)
        return f"{REV}{body}{' ' * pad}...{R}"
    tok = fit_entry(token, max(1, cols - 2))
    body = "  " + tok
    pad = max(0, cols - len(body))
    return f"{REV}{body}{' ' * pad}{R}"


def _row_line(rec, cols, selected_name, hint):
    if selected_name and rec["name"] == selected_name:
        return _bar(rec["token"], cols, hint)
    return rec["line"]


def entry_at_row(entries, rows, r):
    """Base name of the selectable entry at 1-based pane row `r`, else None."""
    if r < 2 or rows < 3:
        return None
    avail = rows - 2
    more = len(entries) > avail
    show_count = (avail - 1) if more else avail
    show_count = max(0, min(show_count, len(entries)))
    idx = r - 2
    if 0 <= idx < show_count:
        return entries[idx]["name"]
    return None


def menu_action_at_row(rows, k, r):
    """Index (0-based) of the menu action at 1-based row `r`, else None.

    Menu layout: rule at `rows-k-1`, actions at `rows-k .. rows-1`, footer `rows`.
    """
    first = rows - k
    idx = r - first
    if 0 <= idx < k:
        return idx
    return None


# --- screen composition ---------------------------------------------------
def title_rule(path, width):
    s = shorten_path(path, max(1, width - 2))
    label = f" {s} "
    fill = max(0, width - len(label))
    return f"{BOLD}{CYAN}{label}{R}{DIM}{'─' * fill}{R}"


def footer(width, n_items, branch):
    count = f"{n_items} {'items' if n_items != 1 else 'item'}"
    plain = [count]
    shown = [f"{DIM}{count}{R}"]
    if branch:
        plain.append(branch)
        shown.append(f"{CYAN}{branch}{R}")
    sep = "  "
    text = sep.join(plain)
    avail = max(1, width - 2)  # reserve " \u2699" (space + gear) at the end
    if len(text) > avail:
        text = ellipsize(text, avail)
        shown = [f"{DIM}{text}{R}"]
    pad = max(0, avail - len(text))
    return sep.join(shown) + " " * pad + f" {DIM}\u2699{R}"


def _menu_lines(title, labels, cols):
    label = f" actions · {title} "
    fill = max(0, cols - len(label))
    out = [f"{MAGENTA}{label}{R}{DIM}{'─' * fill}{R}"]
    for i, lab in enumerate(labels, 1):
        plain = f" {i}  {lab}"
        lab2 = lab if len(plain) <= cols else ellipsize(lab, max(1, cols - 4))
        out.append(f" {BOLD}{CYAN}{i}{R}  {lab2}")
    return out


def compose(title_path, entries, branch, cols, rows,
            selected_name=None, hint=True, menu=None):
    """Assemble a full `rows`-line screen: title rule, entries, (menu), footer."""
    if rows <= 0:
        return ""
    if rows == 1:
        return ellipsize(title_path, cols)
    title = title_rule(title_path, cols)
    if rows == 2:
        return title + "\n" + footer(cols, len(entries), branch)
    labels = menu["labels"] if menu else []
    k = len(labels)
    entry_rows = max(0, rows - 2 - (k + 1 if menu else 0))
    more = len(entries) > entry_rows
    show_count = (entry_rows - 1) if (more and entry_rows >= 1) else entry_rows
    show_count = max(0, min(show_count, len(entries)))
    body = [_row_line(entries[i], cols, selected_name, hint) for i in range(show_count)]
    if more and entry_rows >= 1:
        body.append(f"{DIM}{ellipsize(f'+{len(entries) - entry_rows} more not shown', cols)}{R}")
    lines = [title] + body
    region = (rows - k - 2) if menu else (rows - 1)
    while len(lines) < region:
        lines.append("")
    if menu:
        lines += _menu_lines(menu["title"], labels, cols)
    lines.append(footer(cols, len(entries), branch))
    return "\n".join(lines[:rows])


def preview_footer(width, offset, avail, total, is_bin):
    if is_bin:
        info = "binary"
    elif total == 0:
        info = "(empty)"
    else:
        info = f"{min(offset + 1, total)}-{min(offset + avail, total)}/{total}"
    text = "preview  " + info
    if len(text) > width:
        text = ellipsize(text, width)
    return f"{DIM}{text}{' ' * max(0, width - len(text))}{R}"


def compose_preview(filename, lines, offset, cols, rows):
    """Title rule = filename; body = a slice of the file; footer = position."""
    if rows <= 0:
        return ""
    if rows == 1:
        return ellipsize(filename, cols)
    title = title_rule(filename, cols)
    avail = rows - 2
    total = len(lines) if lines else 0
    if lines is None:
        body = [f"{DIM}(binary or unreadable){R}"]
    else:
        body = [ellipsize(ln, cols) for ln in lines[offset:offset + avail]]
    while len(body) < avail:
        body.append("")
    out = [title] + body
    out.append(preview_footer(cols, offset, avail, total, lines is None))
    return "\n".join(out[:rows])


# --- menu actions ---------------------------------------------------------
def _extract_pane_id(obj):
    if isinstance(obj, dict):
        if "pane_id" in obj:
            return obj["pane_id"]
        for v in obj.values():
            r = _extract_pane_id(v)
            if r:
                return r
    return None


def copy_to_clipboard(text: str) -> bool:
    for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"],
                ["xsel", "--clipboard", "--input"]):
        try:
            if shutil.which(cmd[0]):
                subprocess.run(cmd, input=text.encode(), capture_output=True)
                return True
        except (subprocess.SubprocessError, OSError):
            pass
    return False


def build_actions(abspath, is_dir, followed_pid, followed_is_shell):
    """Action list for the menu. `src` is the pane a new split attaches to."""
    src = followed_pid or ME_ID
    acts = []
    if is_dir:
        acts.append({"label": "Open in new pane", "op": "open_pane",
                     "path": abspath, "is_dir": True, "src": src})
        if followed_is_shell:
            acts.append({"label": "cd into followed pane", "op": "cd_into",
                         "path": abspath, "pid": followed_pid})
    else:
        acts.append({"label": f"Open in new pane ({OPENER.split()[0]})", "op": "open_pane",
                     "path": abspath, "is_dir": False, "src": src})
    acts.append({"label": "Copy path", "op": "copy", "path": abspath})
    return acts


def build_menu(kind, cwd, name, entries, followed_pid):
    """Return {title, actions} for an entry, or None if no actions apply."""
    if kind != "local" or not cwd:
        return None
    rec = next((e for e in entries if e["name"] == name), None)
    if not rec:
        return None
    base = rec["token"].rstrip(_INDICATORS)
    abspath = os.path.join(cwd, base)
    fshell = is_shell(fg_info(followed_pid)[0]) if followed_pid else False
    return {
        "title": base + ("/" if rec["dir"] else ""),
        "actions": build_actions(abspath, rec["dir"], followed_pid, fshell),
    }


def settings_actions():
    """Toggle items shown in the footer-gear settings overlay."""
    mark = lambda on: "\u2713" if on else "\u2022"
    return [
        {"label": f"{mark(_OPT['hidden'])} Show hidden files", "op": "toggle", "key": "hidden"},
        {"label": f"{mark(_OPT['git'])} Git status", "op": "toggle", "key": "git"},
        {"label": f"{mark(_OPT['dirs'])} Dirs first", "op": "toggle", "key": "dirs"},
        {"label": f"{mark(_OPT['mtime'])} Last edited first", "op": "toggle", "key": "mtime"},
    ]


def open_default(path):
    """Open a path with the system default app (macOS `open` / Linux `xdg-open`)."""
    cmd = ["open"] if sys.platform == "darwin" else ["xdg-open"]
    try:
        subprocess.Popen(cmd + [path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        _dbg(f"open_default {path}: {e}")


def run_action(act):
    """Execute a menu action. Failures are swallowed (best-effort)."""
    try:
        op = act.get("op")
        if op == "toggle":
            _OPT[act["key"]] = not _OPT.get(act["key"], True)
        elif op == "copy":
            copy_to_clipboard(act["path"])
        elif op == "cd_into":
            subprocess.run(
                [HERDR, "pane", "run", act["pid"], "cd " + shlex.quote(act["path"])],
                capture_output=True,
            )
        elif op == "open_pane":
            target_cwd = act["path"] if act["is_dir"] else (os.path.dirname(act["path"]) or ".")
            r = subprocess.run(
                [HERDR, "pane", "split", "--pane", act["src"], "--direction", "down",
                 "--cwd", target_cwd, "--no-focus"],
                capture_output=True, text=True,
            )
            new_id = None
            try:
                new_id = _extract_pane_id(json.loads(r.stdout))
            except (ValueError, AttributeError):
                pass
            if not act["is_dir"] and new_id:
                subprocess.run(
                    [HERDR, "pane", "run", new_id,
                     f"{OPENER} {shlex.quote(os.path.basename(act['path']))}"],
                    capture_output=True,
                )
    except Exception as e:  # action dispatch spans subprocess + json + paths
        _dbg(f"run_action {act.get('op')}: {e}")


_preview_cache: OrderedDict[str, tuple[float, list[str] | None]] = OrderedDict()


def read_text_file(path, max_lines=_PREVIEW_MAX_LINES, max_bytes=_PREVIEW_MAX_BYTES):
    """Return text lines of a file, or None if it is binary / unreadable.

    Capped by both line count and total bytes (a single huge line won't blow
    memory).
    """
    try:
        with open(path, "rb") as f:
            if b"\x00" in f.read(2048):
                return None
        lines = []
        total = 0
        truncated = False
        with open(path, "r", errors="replace") as f:
            for _ in range(max_lines):
                ln = f.readline()
                if not ln:
                    break
                total += len(ln)
                lines.append(ln.rstrip("\n").replace("\t", "    "))
                if total >= max_bytes:
                    truncated = True
                    break
        if truncated:
            lines.append("… (truncated)")
        return lines
    except OSError:
        return None


def get_preview_lines(path):
    """Cached text lines for `path`, keyed on mtime. Bounded LRU."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    cached = _preview_cache.get(path)
    if cached and cached[0] == mtime:
        _preview_cache.move_to_end(path)
        return cached[1]
    lines = read_text_file(path)
    _preview_cache[path] = (mtime, lines)
    _preview_cache.move_to_end(path)
    while len(_preview_cache) > _PREVIEW_CACHE_CAP:
        _preview_cache.popitem(last=False)
    return lines


# --- main loop ------------------------------------------------------------
def main():
    last = {"pid": None, "cwd": None, "kind": None, "ssh": None}
    last_render = ""
    stale = True
    # --- view / interaction state -------------------------------------------
    # The "mode" is the product of these flags (no enum: they aren't mutually
    # exclusive — settings layers over the menu overlay, browse_cwd layers over
    # the listing/preview). Input handlers (handle_click/handle_arrow/
    # handle_enter) branch on them. `last` holds the followed pane so self-focus
    # can keep showing it.
    selected_name: str | None = None   # entry name selected in the listing
    selected_path: str | None = None   # the view path `selected_name` belongs to
    preview: str | None = None         # abspath being previewed (in-pane viewer)
    preview_offset = 0
    browse_cwd: str | None = None      # self-focused + local: overrides followed cwd
    menu_open = False                  # bottom overlay open (action menu OR settings)
    menu_actions: list = []
    menu_title = ""
    settings_open = False              # the overlay is the settings panel
    inbuf = b""

    narrow_self()
    # hide cursor, disable line wrap, enable SGR mouse reporting (press/release)
    sys.stdout.write("\033[?25l\033[?7l\033[?1000h\033[?1006h")
    sys.stdout.flush()

    try:
        stdin_fd = sys.stdin.buffer.fileno()
    except (OSError, ValueError):
        stdin_fd = -1

    # Put the PTY into non-canonical mode so mouse/keypress bytes arrive
    # per-read instead of being line-buffered: a mouse click carries no
    # newline, so cooked mode would hold it forever and we'd never see it.
    # OPOST is left ON so our "\n" still maps to CRLF on the screen.
    saved_tc = None
    if stdin_fd >= 0:
        try:
            saved_tc = termios.tcgetattr(stdin_fd)
            tc = termios.tcgetattr(stdin_fd)
            tc[3] = tc[3] & ~termios.ICANON & ~termios.ECHO  # lflag
            tc[6][termios.VMIN] = 0
            tc[6][termios.VTIME] = 0
            termios.tcsetattr(stdin_fd, termios.TCSANOW, tc)
        except OSError:
            saved_tc = None

    def restore():
        if saved_tc is not None:
            try:
                termios.tcsetattr(stdin_fd, termios.TCSANOW, saved_tc)
            except OSError:
                pass
        sys.stdout.write("\033[?1006l\033[?1000l\033[?7h\033[?25h")
        sys.stdout.flush()

    def move(delta):
        """Move the selection by `delta` entries (clamped)."""
        nonlocal selected_name, stale
        names = [e["name"] for e in entries if e["name"]]
        if not names:
            return
        cur = names.index(selected_name) if selected_name in names else None
        cur = 0 if cur is None else max(0, min(len(names) - 1, cur + delta))
        selected_name = names[cur]
        stale = True

    def _selected():
        """(abspath, rec) of the current selection, or (None, None)."""
        if not cwd:
            return None, None
        rec = next((e for e in entries if e["name"] == selected_name), None)
        if not rec:
            return None, None
        return os.path.join(cwd, rec["token"].rstrip(_INDICATORS)), rec

    def handle_click(x, y):
        nonlocal selected_name, menu_open, menu_actions, menu_title
        nonlocal settings_open, preview, preview_offset, stale
        if y == rows and x >= cols - 1:  # footer gear -> settings
            settings_open = menu_open = True
            menu_title = "settings"
            menu_actions = settings_actions()
            stale = True
            return
        if preview:  # any click exits the in-pane preview
            preview = None
            preview_offset = 0
            stale = True
            return
        if menu_open:
            idx = menu_action_at_row(rows, len(menu_actions), y)
            if idx is not None:
                run_action(menu_actions[idx])
                if not settings_open:  # action menu closes; settings stays open
                    menu_open = False
            else:
                menu_open = False
                settings_open = False
            stale = True
            return
        hit = entry_at_row(entries, rows, y)
        if not hit:
            return
        if hit == selected_name:  # second click on the selected entry -> menu
            mnu = build_menu(kind, cwd, hit, entries, last.get("pid"))
            if mnu:
                menu_actions = mnu["actions"]
                menu_title = mnu["title"]
                menu_open = True
                settings_open = False
            stale = True
        else:
            selected_name = hit
            stale = True

    def handle_arrow(c):
        """c is b'A'/'B'/'C'/'D' (up/down/right/left)."""
        nonlocal preview, preview_offset, browse_cwd, stale
        if preview:
            if c == b"A":
                preview_offset = max(0, preview_offset - 1)
                stale = True
            elif c == b"B":
                preview_offset += 1
                stale = True
            elif c == b"D":  # left: leave the preview
                preview = None
                preview_offset = 0
                stale = True
            return
        if c == b"A":
            move(-1)
        elif c == b"B":
            move(1)
        elif c == b"C" and kind == "local" and cwd:
            path, rec = _selected()
            if rec:
                if rec["dir"]:
                    browse_cwd = path             # descend
                else:
                    preview = path               # in-pane preview
                    preview_offset = 0
                stale = True
        elif c == b"D" and kind == "local" and cwd:
            browse_cwd = os.path.dirname(cwd) or cwd  # ascend
            stale = True

    def handle_enter():
        nonlocal browse_cwd, stale
        if menu_open or preview or kind != "local" or not cwd:
            return
        path, rec = _selected()
        if not rec:
            return
        if rec["dir"]:
            browse_cwd = path                    # descend
        else:
            open_default(path)                   # system default app
        stale = True

    try:
        while True:
            panes = pane_entries()
            focused = find_focused(panes)

            me = (
                next((p for p in panes if p.get("pane_id") == ME_ID), None)
                if ME_ID
                else None
            )
            visible = (
                bool(me and focused and me.get("tab_id") == focused.get("tab_id"))
                if ME_ID
                else True
            )
            if not visible:
                stale = True
                time.sleep(INTERVAL)
                continue

            pane = focused
            pid = pane.get("pane_id") if pane else None
            name, argv, proc_cwd = fg_info(pid)
            osc_cwd = pane.get("cwd") if pane else None

            self_focused = bool(ME_ID and pid == ME_ID) or (SELF_TOKEN in argv)
            if not self_focused:
                browse_cwd = None  # leaving the sidebar resumes following

            kind = cwd = ssh_target = None

            if self_focused:
                # The filelist pane itself is focused: hold the last real view.
                # NOTE: pid/cwd/kind are intentionally rebound here from "the
                # focused pane" to "the followed view" — the rest of the loop
                # treats them as the current view's source.
                pid = last["pid"]
                kind = last["kind"]
                ssh_target = last.get("ssh")
                cwd = browse_cwd if (browse_cwd and kind == "local") else last["cwd"]
            elif SSH_RE.search(name + " " + argv):
                dest = ssh_dest(argv)
                if dest:
                    kind, cwd, ssh_target = "ssh", dest, dest

            if not kind:
                if osc_cwd:
                    kind, cwd = "local", osc_cwd
                elif proc_cwd:
                    kind, cwd = "local", proc_cwd

            cols, rows = pane_size()
            title_path = cwd or "(no cwd)"
            entries: list = []
            branch = None

            if kind == "local" and cwd:
                if not os.path.isdir(cwd):
                    entries = _msg("(not a directory)")
                else:
                    gmap, branch = (git_info(cwd) if _OPT["git"] else ({}, None))
                    ls_flag = ("-FA1" if _OPT["hidden"] else "-F1") + ("t" if _OPT["mtime"] else "")
                    try:
                        raw = subprocess.run(
                            ["ls", ls_flag], cwd=cwd,
                            capture_output=True, text=True,
                        ).stdout
                    except OSError:
                        raw = ""
                    entries = render_entries(raw, gmap, cols, cwd) or _msg("(empty)")
            elif kind == "ssh" and ssh_target:
                rpath = remote_cwd_from_pane(pid) if pid else None
                title_path = f"{ssh_target}:{rpath}" if rpath else f"{ssh_target}:~"
                raw = render_remote(ssh_target, rpath)
                entries = render_entries(raw, {}, cols) or _msg("(no listing)")
            else:
                entries = _msg("focus a pane with a working directory")

            # selection + menu are only valid for the current view
            if selected_path != title_path:
                selected_name = None
                selected_path = title_path
                if not settings_open:  # settings (display prefs) survive a view change
                    menu_open = False
                preview = None
                preview_offset = 0

            # when this pane is focused, ensure there's a selection to navigate
            if self_focused and not selected_name and entries and entries[0]["name"]:
                selected_name = entries[0]["name"]
                stale = True

            if pid and kind:
                last = {"pid": pid, "cwd": cwd, "kind": kind, "ssh": ssh_target}

            if settings_open:
                menu_actions = settings_actions()
                menu_title = "settings"
            k = len(menu_actions) if menu_open else 0
            menu = ({"title": menu_title, "labels": [a["label"] for a in menu_actions]}
                    if menu_open else None)
            hint = kind == "local"
            if preview:
                plines = get_preview_lines(preview) if kind == "local" else None
                total = len(plines) if plines else 0
                if preview_offset > total:
                    preview_offset = max(0, total - 1)
                screen = compose_preview(os.path.basename(preview), plines,
                                         preview_offset, cols, rows)
            else:
                screen = compose(title_path, entries, branch, cols, rows,
                                 selected_name, hint, menu)
            if stale or screen != last_render:
                stale = False
                last_render = screen
                sys.stdout.write("\033[2J\033[H" + screen)
                sys.stdout.flush()

            # wait for either the next poll tick or a mouse click on stdin
            rdy = select.select([stdin_fd], [], [], INTERVAL)[0] if stdin_fd >= 0 else []
            if rdy:
                try:
                    chunk = os.read(stdin_fd, 4096)
                except OSError:
                    chunk = b""
                if chunk:
                    inbuf += chunk
                    end = 0
                    for m in MOUSE_RE.finditer(inbuf):
                        end = m.end()
                        if m.group(4) == b"M" and (int(m.group(1)) & 3) == 0:
                            handle_click(int(m.group(2)), int(m.group(3)))
                    inbuf = inbuf[end:]
                    for km in KEY_RE.finditer(inbuf):
                        if not menu_open:
                            handle_arrow(km.group()[-1:])
                    inbuf = KEY_RE.sub(b"", inbuf)
                    if b"\r" in inbuf or b"\n" in inbuf:  # Enter
                        inbuf = (inbuf.replace(b"\r", b"", 1) if b"\r" in inbuf
                                 else inbuf.replace(b"\n", b"", 1))
                        handle_enter()
                    # discard non-escape leftovers; keep a possible partial seq
                    if inbuf and not inbuf.startswith(b"\x1b"):
                        inbuf = b""
                    if len(inbuf) > _STDIN_BUF_CAP:
                        inbuf = inbuf[-_STDIN_BUF_CAP:]
                else:
                    time.sleep(0.1)  # EOF on stdin (pane closing) — avoid busy loop
    except KeyboardInterrupt:
        pass
    finally:
        restore()


if __name__ == "__main__":
    main()

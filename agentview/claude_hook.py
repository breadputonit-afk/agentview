"""Claude Code hook integration for agentview."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except AttributeError:
        pass

from rich.console import Console
from rich.rule import Rule
from rich.table import Table

ICONS = {
    "pending":     "[dim]○[/dim]",
    "running":     "[bold yellow]▶[/bold yellow]",
    "done":        "[bold green]✓[/bold green]",
    "failed":      "[bold red]✗[/bold red]",
    "interrupted": "[bold yellow]~[/bold yellow]",
}

console = Console(stderr=True, highlight=False)

POINTER_PATH = Path(tempfile.gettempdir()) / "agentview_current.txt"
LOG_PATH = Path.home() / ".claude" / "agentview_log.jsonl"
TASKS_DIR = Path.home() / ".claude" / "tasks"
CONFIG_PATH = Path.home() / ".claude" / "agentview_config.json"

_DEFAULT_CONFIG = {
    "show_sources": True,
    "toast": True,
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return {**_DEFAULT_CONFIG, **data}
        except Exception:
            pass
    return dict(_DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def _state_path(session_id: str) -> Path:
    return Path(tempfile.gettempdir()) / f"agentview_{session_id}.json"


def _load_state(session_id: str) -> dict:
    path = _state_path(session_id)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"session_id": session_id, "started_at": time.time(), "steps": []}


def _save_state(state: dict) -> None:
    target = _state_path(state["session_id"])
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)


def _lock_path(session_id: str) -> Path:
    return Path(tempfile.gettempdir()) / f"agentview_{session_id}.lock"


@contextmanager
def _state_lock(session_id: str, timeout: float = 2.0):
    """File-based mutex so concurrent async hook processes don't race on the state file."""
    lock = _lock_path(session_id)
    deadline = time.monotonic() + timeout
    acquired = False
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            acquired = True
            break
        except FileExistsError:
            if time.monotonic() > deadline:
                # Stale lock from a crashed process — force-delete so future hooks can proceed
                try:
                    lock.unlink()
                except Exception:
                    pass
                break
            time.sleep(0.01)
    try:
        yield
    finally:
        if acquired:
            try:
                lock.unlink()
            except Exception:
                pass


def _turns_path(session_id: str) -> Path:
    return Path(tempfile.gettempdir()) / f"agentview_turns_{session_id}.json"


def load_turns(session_id: str) -> list[list[dict]]:
    """Return completed turns for this session; each turn is a list of step dicts."""
    path = _turns_path(session_id)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _append_turn(state: dict) -> None:
    turns = load_turns(state["session_id"])
    turns.append([{k: v for k, v in s.items() if k != "started_at"} for s in state["steps"]])
    path = _turns_path(state["session_id"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(turns, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _task_cache_path(session_id: str) -> Path:
    return Path(tempfile.gettempdir()) / f"agentview_tasks_{session_id}.json"


_task_dir_mtime: dict[str, float] = {}  # session_id → last seen mtime
_task_cache: dict[str, list[dict]] = {}  # session_id → last scanned tasks


def load_tasks(session_id: str) -> list[dict]:
    """Read task files; fall back to cache when Claude Code has cleaned them up.
    Uses directory mtime to skip redundant scans at 4 Hz."""
    task_dir = TASKS_DIR / session_id
    if task_dir.is_dir():
        try:
            mtime = task_dir.stat().st_mtime
        except OSError:
            mtime = 0.0
        if mtime != _task_dir_mtime.get(session_id):
            # Directory changed — re-scan
            tasks = []
            for f in task_dir.iterdir():
                if f.suffix == ".json":
                    try:
                        t = json.loads(f.read_text(encoding="utf-8"))
                        if t.get("status") != "deleted":
                            tasks.append(t)
                    except Exception:
                        pass
            if tasks:
                tasks.sort(key=lambda t: int(t.get("id", 0)))
                _task_dir_mtime[session_id] = mtime
                _task_cache[session_id] = tasks
                try:
                    cache = _task_cache_path(session_id)
                    tmp = cache.with_suffix(".tmp")
                    tmp.write_text(json.dumps(tasks, ensure_ascii=False), encoding="utf-8")
                    tmp.replace(cache)
                except Exception:
                    pass
                return tasks
        elif session_id in _task_cache:
            return _task_cache[session_id]

    # Fall back to persistent cache on disk
    cache = _task_cache_path(session_id)
    if cache.exists():
        try:
            tasks = json.loads(cache.read_text(encoding="utf-8-sig"))
            _task_cache[session_id] = tasks
            return tasks
        except Exception:
            pass
    return []


def _packages_path(session_id: str) -> Path:
    return Path(tempfile.gettempdir()) / f"agentview_packages_{session_id}.json"


def _parse_install_cmd(command: str) -> list[tuple[str, str]]:
    """Return (manager, package) pairs from an install command."""
    import re
    import shlex
    results: list[tuple[str, str]] = []
    cmd = command.strip()

    patterns = [
        (r"pip(?:3)?\s+install\s+(.*)", "pip"),
        (r"conda\s+install\s+(.*)",     "conda"),
        (r"npm\s+install\s+(.*)",       "npm"),
        (r"scoop\s+install\s+(.*)",     "scoop"),
        (r"winget\s+install\s+(.*)",    "winget"),
    ]
    for pattern, manager in patterns:
        m = re.match(pattern, cmd, re.IGNORECASE)
        if not m:
            continue
        try:
            args = shlex.split(m.group(1))
        except ValueError:
            args = m.group(1).split()
        skip_next = False
        for arg in args:
            if skip_next:
                skip_next = False
                continue
            if arg in ("-r", "--requirement", "-c", "--constraint", "-e", "--editable"):
                skip_next = True
                continue
            if arg.startswith("-"):
                continue
            if arg.endswith(".txt") or arg.endswith(".cfg"):
                continue
            pkg = re.split(r"[>=<!;\[]", arg)[0].strip()
            if pkg and re.match(r'^[A-Za-z0-9][A-Za-z0-9._\-]*$', pkg):
                results.append((manager, pkg))
        return results
    return results


def _save_packages(session_id: str, pkgs: list[tuple[str, str]]) -> None:
    path = _packages_path(session_id)
    existing: list[dict] = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    seen = {(p["manager"], p["package"]) for p in existing}
    for manager, package in pkgs:
        if (manager, package) not in seen:
            existing.append({"manager": manager, "package": package})
            seen.add((manager, package))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_packages(session_id: str) -> list[dict]:
    path = _packages_path(session_id)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _web_source(tool_name: str, tool_input: dict) -> str | None:
    if tool_name == "WebFetch":
        return tool_input.get("url") or None
    if tool_name == "WebSearch":
        return tool_input.get("query") or None
    return None


def _input_summary(tool_name: str, tool_input: dict) -> str:
    if tool_name in ("Read", "Write", "Edit", "NotebookEdit"):
        p = tool_input.get("file_path", "")
        return Path(p).name if p else ""
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return (cmd[:48] + "…") if len(cmd) > 48 else cmd
    if tool_name in ("Glob", "Grep"):
        return tool_input.get("pattern", "")
    if tool_name == "WebFetch":
        url = tool_input.get("url", "")
        return (url[:48] + "…") if len(url) > 48 else url
    if tool_name == "WebSearch":
        q = tool_input.get("query", "")
        return (q[:48] + "…") if len(q) > 48 else q
    if tool_name == "Agent":
        return tool_input.get("description", "")[:48]
    return ""


def _build_table(steps: list[dict]) -> Table:
    table = Table.grid(padding=(0, 2))
    table.add_column(width=2)
    table.add_column(min_width=16)
    table.add_column(min_width=30)
    table.add_column(width=8, justify="right")

    for s in steps:
        icon = ICONS.get(s["status"], "○")
        elapsed_str = ""
        if s.get("elapsed") is not None:
            color = "red" if s["status"] == "failed" else "dim"
            elapsed_str = f"[{color}]{s['elapsed']:.1f}s[/{color}]"
        elif s["status"] == "running":
            live = time.time() - s.get("started_at", time.time())
            elapsed_str = f"[dim]{live:.0f}s[/dim]"
        table.add_row(
            icon,
            s["tool"],
            f"[dim]{s.get('input_summary', '')}[/dim]",
            elapsed_str,
        )

    return table


def on_pre_tool_use(session_id: str, payload: dict) -> None:
    tool_name = payload.get("tool_name", "Unknown")
    tool_input = payload.get("tool_input", {})
    with _state_lock(session_id):
        state = _load_state(session_id)
        step: dict = {
            "tool": tool_name,
            "input_summary": _input_summary(tool_name, tool_input),
            "status": "running",
            "started_at": time.time(),
            "elapsed": None,
        }
        src = _web_source(tool_name, tool_input)
        if src:
            step["source"] = src
        state["steps"].append(step)
        _save_state(state)
        if tool_name in ("Bash", "PowerShell"):
            pkgs = _parse_install_cmd(tool_input.get("command", ""))
            if pkgs:
                _save_packages(session_id, pkgs)
    try:
        POINTER_PATH.write_text(session_id, encoding="utf-8")
    except Exception:
        pass


def on_post_tool_use(session_id: str, payload: dict) -> None:
    tool_name = payload.get("tool_name", "Unknown")
    tool_response = payload.get("tool_response", {})
    is_error = isinstance(tool_response, dict) and tool_response.get("is_error", False)
    with _state_lock(session_id):
        state = _load_state(session_id)
        for step in reversed(state["steps"]):
            if step["tool"] == tool_name and step["status"] == "running":
                step["elapsed"] = time.time() - step["started_at"]
                step["status"] = "failed" if is_error else "done"
                break
        _save_state(state)


def _append_log(state: dict, total_time: float) -> None:
    entry = {
        "session_id": state["session_id"],
        "started_at": state["started_at"],
        "total_time": total_time,
        "steps": [
            {k: v for k, v in s.items() if k != "started_at"}
            for s in state["steps"]
        ],
    }
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _win_toast(title: str, body: str) -> None:
    if sys.platform != "win32":
        return
    try:
        # Pass strings via env vars to avoid any PowerShell string-injection
        script = (
            "[Windows.UI.Notifications.ToastNotificationManager,"
            " Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
            "[Windows.Data.Xml.Dom.XmlDocument,"
            " Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null;"
            "$d = [Windows.UI.Notifications.ToastNotificationManager]::"
            "GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
            "$d.GetElementsByTagName('text')[0].AppendChild($d.CreateTextNode($env:_AV_TITLE)) | Out-Null;"
            "$d.GetElementsByTagName('text')[1].AppendChild($d.CreateTextNode($env:_AV_BODY)) | Out-Null;"
            "$n = [Windows.UI.Notifications.ToastNotification]::new($d);"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('agentview').Show($n)"
        )
        env = os.environ.copy()
        env["_AV_TITLE"] = title
        env["_AV_BODY"] = body
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=5, env=env,
        )
    except Exception:
        pass


def on_stop(state: dict) -> None:
    for step in state["steps"]:
        if step["status"] == "running":
            step["elapsed"] = time.time() - step["started_at"]
            step["status"] = "interrupted"

    total_time = time.time() - state["started_at"]

    if state["steps"]:
        failed = sum(1 for s in state["steps"] if s["status"] == "failed")
        interrupted = sum(1 for s in state["steps"] if s["status"] == "interrupted")

        console.print()
        console.print(Rule("[bold cyan]Claude Code Session[/bold cyan]", style="dim"))
        console.print(_build_table(state["steps"]))

        parts = [f"[dim]{len(state['steps'])} tools[/dim]"]
        if failed:
            parts.append(f"[bold red]{failed} failed[/bold red]")
        if interrupted:
            parts.append(f"[bold yellow]{interrupted} interrupted[/bold yellow]")
        parts.append(f"[dim]{total_time:.1f}s total[/dim]")
        console.print("  " + "  ·  ".join(parts))

        cfg = load_config()
        searches: list[str] = []
        fetches: list[str] = []
        seen: set[str] = set()
        for s in state["steps"]:
            src = s.get("source")
            if not src or src in seen:
                continue
            seen.add(src)
            if s["tool"] == "WebSearch":
                searches.append(src)
            elif s["tool"] == "WebFetch":
                fetches.append(src)

        if cfg["show_sources"] and (searches or fetches):
            console.print()
            console.print("  [dim]Sources[/dim]")
            for q in searches:
                console.print(f"  [dim]🔍  {q}[/dim]")
            for url in fetches:
                try:
                    from urllib.parse import urlparse
                    domain = urlparse(url).netloc or url
                except Exception:
                    domain = url
                console.print(f"  🌐  [link={url}]{domain}[/link]")

        console.print()

        _append_log(state, total_time)
        try:
            _append_turn(state)
        except Exception:
            pass

        if cfg["toast"]:
            toast_parts = [f"{len(state['steps'])} tools", f"{total_time:.1f}s"]
            if failed:
                toast_parts.append(f"{failed} failed")
            _win_toast("Claude Code 完成", " · ".join(toast_parts))

    _state_path(state["session_id"]).unlink(missing_ok=True)
    try:
        if POINTER_PATH.read_text(encoding="utf-8").strip() == state["session_id"]:
            POINTER_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="agentview Claude Code hook")
    parser.add_argument(
        "--event",
        required=True,
        choices=["PreToolUse", "PostToolUse", "Stop", "SubagentStop", "Notification"],
    )
    args = parser.parse_args()

    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
    except Exception:
        payload = {}

    session_id = payload.get("session_id", "default")

    if args.event == "PreToolUse":
        on_pre_tool_use(session_id, payload)
    elif args.event == "PostToolUse":
        on_post_tool_use(session_id, payload)
    elif args.event in ("Stop", "SubagentStop"):
        on_stop(_load_state(session_id))


if __name__ == "__main__":
    main()

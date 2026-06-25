"""CLI for agentview."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from rich.console import Console, Group
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

console = Console(highlight=False)


def install_hooks() -> None:
    settings_path = Path.home() / ".claude" / "settings.json"

    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"Error: {settings_path} is not valid JSON: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        settings = {}

    python = sys.executable
    base_cmd = f'"{python}" -m agentview.claude_hook'

    hook_specs = [
        ("PreToolUse",    f"{base_cmd} --event PreToolUse",    True),
        ("PostToolUse",   f"{base_cmd} --event PostToolUse",   True),
        ("Stop",          f"{base_cmd} --event Stop",          False),
        ("SubagentStop",  f"{base_cmd} --event SubagentStop",  False),
    ]

    hooks = settings.setdefault("hooks", {})
    added: list[str] = []

    for event, cmd, is_async in hook_specs:
        event_list = hooks.setdefault(event, [])
        already = any(
            "agentview" in h.get("command", "")
            for group in event_list
            for h in group.get("hooks", [])
        )
        if already:
            print(f"  skip   {event} (already configured)")
            continue

        hook_entry: dict = {"type": "command", "command": cmd}
        if is_async:
            hook_entry["async"] = True

        event_list.append({"hooks": [hook_entry]})
        added.append(event)
        print(f"  added  {event}")

    if added:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nHooks installed → {settings_path}")
        print("Restart Claude Code to activate.")
    else:
        print("\nAll hooks already configured.")


def update_hooks() -> None:
    settings_path = Path.home() / ".claude" / "settings.json"

    if not settings_path.exists():
        print("~/.claude/settings.json not found. Run: agentview install-hooks")
        sys.exit(1)

    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"Error: {settings_path} is not valid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    python = sys.executable
    base_cmd = f'"{python}" -m agentview.claude_hook'

    hooks = settings.get("hooks", {})
    updated: list[str] = []
    missing: list[str] = []

    for event in ("PreToolUse", "PostToolUse", "Stop", "SubagentStop"):
        event_list = hooks.get(event, [])
        found = False
        for group in event_list:
            for hook in group.get("hooks", []):
                cmd = hook.get("command", "")
                if "agentview" in cmd and "claude_hook" in cmd:
                    new_cmd = f"{base_cmd} --event {event}"
                    if cmd == new_cmd:
                        print(f"  up-to-date  {event}")
                    else:
                        hook["command"] = new_cmd
                        updated.append(event)
                        print(f"  updated     {event}")
                    found = True
                    break
            if found:
                break
        if not found:
            missing.append(event)
            print(f"  missing     {event}")

    if missing:
        print(f"\n⚠  {len(missing)} hook(s) not found — run: agentview install-hooks")

    if updated:
        settings_path.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nHooks updated → {settings_path}")
        print("Restart Claude Code to activate.")
    elif not missing:
        print("\nAll hooks already up to date.")


def remove_hooks() -> None:
    settings_path = Path.home() / ".claude" / "settings.json"

    if not settings_path.exists():
        print("~/.claude/settings.json not found.")
        return

    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    hooks = settings.get("hooks", {})
    removed: list[str] = []

    for event, event_list in list(hooks.items()):
        filtered = [
            group for group in event_list
            if not any("agentview" in h.get("command", "") for h in group.get("hooks", []))
        ]
        if len(filtered) != len(event_list):
            removed.append(event)
        if filtered:
            hooks[event] = filtered
        else:
            del hooks[event]

    settings_path.write_text(
        json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    if removed:
        for event in removed:
            print(f"  removed  {event}")
        print(f"\nHooks removed from {settings_path}")
        print("Restart Claude Code to apply.")
    else:
        print("No agentview hooks found.")


def watch() -> None:
    from rich.live import Live
    from agentview.claude_hook import POINTER_PATH, _load_state, _build_table, load_tasks

    TASK_ICONS = {
        "completed":  "[bold green]✓[/bold green]",
        "in_progress": "[bold yellow]▶[/bold yellow]",
        "pending":    "[dim]○[/dim]",
    }

    def _task_panel(tasks: list[dict]) -> object | None:
        if not tasks:
            return None
        done = sum(1 for t in tasks if t["status"] == "completed")
        total = len(tasks)
        pct = done / total if total else 0
        bar_filled = int(pct * 20)
        bar = "[bold green]" + "█" * bar_filled + "[/bold green]" + "[dim]" + "░" * (20 - bar_filled) + "[/dim]"

        header = Text.from_markup(
            f"[bold]Tasks[/bold]  {bar}  [dim]{done}/{total}[/dim]"
        )
        task_table = Table.grid(padding=(0, 1))
        task_table.add_column(width=2)
        task_table.add_column()
        for t in tasks:
            icon = TASK_ICONS.get(t["status"], "[dim]○[/dim]")
            subject = t.get("subject", "")
            if t["status"] == "completed":
                subject = f"[dim]{subject}[/dim]"
            elif t["status"] == "in_progress":
                subject = f"[bold]{subject}[/bold]"
            task_table.add_row(icon, subject)
        return Group(Text(""), Rule(style="dim"), header, task_table)

    def _renderable(state: dict | None, tasks: list[dict]) -> object:
        if state is None and not tasks:
            return Text.from_markup("[dim]Waiting for Claude Code session...[/dim]")
        if state is None:
            parts: list[object] = [Text.from_markup("[dim]Waiting for next turn...[/dim]")]
            task_panel = _task_panel(tasks)
            if task_panel:
                parts.append(task_panel)
            return Group(*parts)
        elapsed = time.time() - state["started_at"]
        header = Text.from_markup(
            f"[bold cyan]Claude Code Session[/bold cyan]  [dim]{elapsed:.0f}s elapsed[/dim]"
        )
        parts = [header]
        if state["steps"]:
            parts += [Text(""), _build_table(state["steps"])]
        else:
            parts.append(Text.from_markup("[dim]Starting...[/dim]"))
        task_panel = _task_panel(tasks)
        if task_panel:
            parts.append(task_panel)
        return Group(*parts)

    try:
        # Seed last_session_id: prefer active POINTER_PATH, fall back to most-recent tasks dir
        last_session_id = ""
        try:
            if POINTER_PATH.exists():
                last_session_id = POINTER_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        if not last_session_id:
            task_base = Path.home() / ".claude" / "tasks"
            if task_base.is_dir():
                dirs = [(d, d.stat().st_mtime) for d in task_base.iterdir() if d.is_dir()]
                if dirs:
                    last_session_id = max(dirs, key=lambda x: x[1])[0].name

        # Only block in spinner when there are no tasks to show yet
        if not last_session_id or not load_tasks(last_session_id):
            with console.status("[dim]Waiting for Claude Code session...[/dim]") as spinner:
                while not POINTER_PATH.exists():
                    time.sleep(0.25)
                spinner.stop()

        with Live(console=console, refresh_per_second=4) as live:
            while True:
                session_id = ""
                try:
                    if POINTER_PATH.exists():
                        session_id = POINTER_PATH.read_text(encoding="utf-8").strip()
                        last_session_id = session_id
                except OSError:
                    pass
                state = _load_state(session_id) if session_id else None
                tasks = load_tasks(last_session_id) if last_session_id else []
                time.sleep(0.25)
                live.update(_renderable(state, tasks))

    except KeyboardInterrupt:
        pass


def _load_sessions() -> list[dict]:
    from agentview.claude_hook import LOG_PATH

    if not LOG_PATH.exists():
        return []
    sessions = []
    for line in LOG_PATH.read_text(encoding="utf-8").strip().splitlines():
        try:
            sessions.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return sessions


def log_cmd(tail: int, tool_filter: str | None) -> None:
    from agentview.claude_hook import _build_table

    if tail <= 0:
        return

    sessions = _load_sessions()
    if not sessions:
        console.print("[dim]No sessions logged yet.[/dim]")
        return

    if tool_filter:
        sessions = [s for s in sessions if any(
            step.get("tool", "").lower() == tool_filter.lower()
            for step in s.get("steps", [])
        )]
        if not sessions:
            console.print(f"[dim]No sessions containing tool '{tool_filter}'.[/dim]")
            return

    for session in reversed(sessions[-tail:]):
        started = datetime.fromtimestamp(session["started_at"]).strftime("%Y-%m-%d %H:%M:%S")
        total_time = session.get("total_time", 0)
        steps = session.get("steps", [])
        failed = sum(1 for s in steps if s.get("status") == "failed")

        parts = [f"{len(steps)} tools", f"{total_time:.1f}s"]
        if failed:
            parts.append(f"[bold red]{failed} failed[/bold red]")

        console.print(
            f"\n[bold]Session[/bold] [dim]{started}[/dim]  "
            f"[dim]({' · '.join(parts)})[/dim]"
        )
        console.print(_build_table(steps))

    from agentview.claude_hook import LOG_PATH
    console.print(f"\n[dim]{len(sessions)} sessions total — {LOG_PATH}[/dim]")


def stats_cmd() -> None:
    from collections import Counter, defaultdict

    sessions = _load_sessions()
    if not sessions:
        console.print("[dim]No sessions logged yet.[/dim]")
        return

    all_steps = [s for sess in sessions for s in sess.get("steps", [])]
    total_tools = len(all_steps)
    if not total_tools:
        console.print("[dim]No tool calls recorded yet.[/dim]")
        return

    failed_steps = [s for s in all_steps if s.get("status") == "failed"]
    failed = len(failed_steps)
    durations = [sess.get("total_time", 0) for sess in sessions]
    avg_duration = sum(durations) / len(durations)
    tool_counts: Counter = Counter(s.get("tool", "?") for s in all_steps)

    console.print(f"\n[bold]agentview stats[/bold]  [dim]({len(sessions)} sessions)[/dim]\n")

    summary = Table.grid(padding=(0, 3))
    summary.add_column()
    summary.add_column()
    summary.add_column()
    summary.add_column()
    summary.add_row(
        f"[bold]{total_tools}[/bold] tool calls",
        f"[bold red]{failed}[/bold red] failed  [dim]({failed/total_tools*100:.0f}%)[/dim]",
        f"avg [bold]{avg_duration:.0f}s[/bold] / session",
        f"longest [bold]{max(durations):.0f}s[/bold]",
    )
    console.print(summary)

    console.print("\n[dim]Top tools[/dim]")
    top = tool_counts.most_common(10)
    bar_max = top[0][1] if top else 1
    for tool, count in top:
        bar_len = int(count / bar_max * 30)
        console.print(f"  {tool:<20} {'█' * bar_len:<30} [dim]{count}[/dim]")

    tool_times: dict[str, list[float]] = defaultdict(list)
    for s in all_steps:
        if s.get("elapsed") is not None and s.get("status") == "done":
            tool_times[s.get("tool", "?")].append(s["elapsed"])
    if tool_times:
        avg_times = sorted(
            ((t, sum(v) / len(v)) for t, v in tool_times.items()),
            key=lambda x: -x[1],
        )[:8]
        time_max = avg_times[0][1]
        console.print("\n[dim]Slowest tools (avg)[/dim]")
        for tool, avg in avg_times:
            bar_len = int(avg / time_max * 30)
            console.print(f"  {tool:<20} {'█' * bar_len:<30} [dim]{avg:.1f}s[/dim]")

    if len(sessions) > 1:
        hour_counts: Counter = Counter(
            datetime.fromtimestamp(sess["started_at"]).hour for sess in sessions
        )
        hour_max = max(hour_counts.values())
        console.print("\n[dim]Activity by hour[/dim]")
        for h in range(24):
            n = hour_counts.get(h, 0)
            if n == 0:
                continue
            bar_len = int(n / hour_max * 20)
            console.print(f"  {h:02d}  {'█' * bar_len} [dim]{n}[/dim]")

    # --- recommendations ---
    console.print("\n[bold]建議[/bold]")
    hints: list[tuple[str, str]] = []  # (level, text)

    bash_total = tool_counts.get("Bash", 0) + tool_counts.get("PowerShell", 0)
    bash_failed = sum(
        1 for s in failed_steps
        if s.get("tool") in ("Bash", "PowerShell")
    )
    bash_fail_pct = bash_failed / bash_total * 100 if bash_total else 0
    if bash_fail_pct > 20:
        hints.append(("warn", f"Bash/PowerShell 失敗率 {bash_fail_pct:.0f}%（> 20%）"
                              " — 考慮在 CLAUDE.md 記錄穩定的指令格式"))
    else:
        hints.append(("ok", f"Bash/PowerShell 失敗率 {bash_fail_pct:.0f}% — 正常"))

    agent_pct = tool_counts.get("Agent", 0) / total_tools * 100
    if agent_pct > 15:
        hints.append(("warn", f"Agent 佔 {agent_pct:.0f}%（> 15%）"
                              " — 任務複雜度高，開場時先拆分步驟再交給 Claude"))
    elif agent_pct > 0:
        hints.append(("ok", f"Agent 佔 {agent_pct:.0f}% — 正常"))

    if avg_duration > 120:
        hints.append(("info", f"平均 session {avg_duration:.0f}s（> 120s）"
                              " — 任務偏長，可嘗試拆成更小的對話"))
    else:
        hints.append(("ok", f"平均 session {avg_duration:.0f}s — 長度適中"))

    read_n = tool_counts.get("Read", 0)
    edit_n = tool_counts.get("Edit", 0)
    if edit_n and read_n / edit_n > 5:
        hints.append(("info", f"Read/Edit 比 {read_n/edit_n:.1f}x（> 5x）"
                              " — Claude 花較多時間探索程式碼，可在 CLAUDE.md 補充重要檔案位置"))
    elif edit_n:
        hints.append(("ok", f"Read/Edit 比 {read_n/edit_n:.1f}x — 正常"))

    web_pct = tool_counts.get("WebSearch", 0) / total_tools * 100
    if web_pct > 10:
        hints.append(("info", f"WebSearch 佔 {web_pct:.0f}%（> 10%）"
                              " — 常需查文件，考慮把常用連結加入 CLAUDE.md"))

    overall_ok_pct = (total_tools - failed) / total_tools * 100
    if overall_ok_pct >= 95:
        hints.append(("ok", f"整體成功率 {overall_ok_pct:.0f}% — 運行狀況良好"))

    LEVEL_ICON = {"warn": "[bold red]⚠[/bold red] ", "info": "[bold yellow]ℹ[/bold yellow] ", "ok": "[bold green]✓[/bold green] "}
    for level, text in hints:
        console.print(f"  {LEVEL_ICON[level]}{text}")

    console.print()


def hook_test() -> None:
    from agentview.claude_hook import LOG_PATH

    session_id = f"hooktest-{uuid.uuid4().hex[:8]}"
    python = sys.executable

    silent_events = [
        ("PreToolUse",  {"session_id": session_id, "tool_name": "Read", "tool_input": {"file_path": "/example/test.py"}}),
        ("PostToolUse", {"session_id": session_id, "tool_name": "Read", "tool_input": {}, "tool_response": {}}),
        ("PreToolUse",  {"session_id": session_id, "tool_name": "Edit", "tool_input": {"file_path": "/example/test.py"}}),
        ("PostToolUse", {"session_id": session_id, "tool_name": "Edit", "tool_input": {}, "tool_response": {}}),
        ("PreToolUse",  {"session_id": session_id, "tool_name": "Bash", "tool_input": {"command": "python -m pytest"}}),
        ("PostToolUse", {"session_id": session_id, "tool_name": "Bash", "tool_input": {}, "tool_response": {"is_error": True}}),
    ]

    console.print("[bold]agentview hook test[/bold]\n")

    errors: list[str] = []
    for event, payload in silent_events:
        result = subprocess.run(
            [python, "-m", "agentview.claude_hook", "--event", event],
            input=json.dumps(payload).encode(),
            capture_output=True,
        )
        if result.returncode != 0:
            errors.append(f"{event} exited {result.returncode}: {result.stderr.decode(errors='replace').strip()}")
            console.print(f"  [bold red]✗[/bold red]  {event}")
        else:
            console.print(f"  [bold green]✓[/bold green]  {event}")
        time.sleep(0.05)

    if errors:
        console.print("\n[bold red]Errors:[/bold red]")
        for e in errors:
            console.print(f"  {e}")
        console.print("\n[bold red]Hook test failed.[/bold red] Is agentview installed? Try: pip install -e .")
        return

    log_lines_before = _count_log_lines(LOG_PATH)

    console.print(f"\n  [dim]Running Stop hook — summary should appear below:[/dim]")
    stop_result = subprocess.run(
        [python, "-m", "agentview.claude_hook", "--event", "Stop"],
        input=json.dumps({"session_id": session_id}).encode(),
    )

    if stop_result.returncode != 0:
        console.print(f"  [bold red]✗[/bold red]  Stop hook exited {stop_result.returncode}")
        console.print("\n[bold red]Hook test failed.[/bold red]")
        return

    log_lines_after = _count_log_lines(LOG_PATH)
    if log_lines_after > log_lines_before:
        console.print(f"  [bold green]✓[/bold green]  Log entry written ({LOG_PATH})")
    else:
        console.print(f"  [bold red]✗[/bold red]  Log entry missing — check write permissions on {LOG_PATH}")
        console.print("\n[bold red]Hook test failed.[/bold red]")
        return

    console.print("\n[bold green]Hook test passed.[/bold green]")


def _count_log_lines(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8").splitlines())
    except Exception:
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="agentview", description="agentview CLI")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("install-hooks", help="Add agentview hooks to ~/.claude/settings.json")
    sub.add_parser("update-hooks", help="Update hook commands to current Python executable")
    sub.add_parser("remove-hooks", help="Remove agentview hooks from ~/.claude/settings.json")
    sub.add_parser("watch", help="Live view of the current Claude Code session")
    log_p = sub.add_parser("log", help="Show recent session history")
    log_p.add_argument("--tail", type=int, default=10, metavar="N", help="Show last N sessions (default: 10)")
    log_p.add_argument("--tool", metavar="NAME", help="Only show sessions containing this tool")
    sub.add_parser("stats", help="Usage statistics and recommendations")
    sub.add_parser("hook-test", help="Simulate a session to verify hook installation")
    args = parser.parse_args()

    if args.command == "install-hooks":
        install_hooks()
    elif args.command == "update-hooks":
        update_hooks()
    elif args.command == "remove-hooks":
        remove_hooks()
    elif args.command == "watch":
        watch()
    elif args.command == "log":
        log_cmd(args.tail, getattr(args, "tool", None))
    elif args.command == "stats":
        stats_cmd()
    elif args.command == "hook-test":
        hook_test()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

# agentview

Visualize your AI agent's step progress in the terminal.

```
 ✓  搜尋資料          1.2s
 ✓  整理結果          0.5s
 ▶  分析內容          ...
 ○  生成報告
```

## Install

```bash
pip install agentview
```

## Usage

**Decorator:**
```python
from agentview import tracker

@tracker.step("Search data")
def search():
    ...
```

**Context manager:**
```python
with tracker.step("Analyze content"):
    ...
```

**Multiple steps with session:**
```python
with tracker.session():
    articles = search()

    with tracker.step("Summarize"):
        ...

    analyze(articles)
```

## Claude Code Integration

Display a summary of every tool Claude used, at the end of each response.

**Install hooks (one-time setup):**

```bash
agentview install-hooks
```

Adds `PreToolUse`, `PostToolUse`, `Stop`, and `SubagentStop` hooks to
`~/.claude/settings.json`. Restart Claude Code to activate.

**What it looks like:**

```
──────────── Claude Code Session ────────────
✓  Read       tracker.py                0.3s
✓  Edit       tracker.py                0.5s
✓  Bash       python example.py         1.2s
   3 tools  ·  2.0s total
─────────────────────────────────────────────
```

**Live view (open in a second terminal):**

```bash
agentview watch
```

Waits for a session to start, then shows a live-updating table of tools in progress.

**Session history:**

```bash
agentview log          # last 10 sessions
agentview log --tail 5 # last 5 sessions
```

**Remove hooks:**

```bash
agentview remove-hooks
```

## Requirements

- Python 3.10+
- [rich](https://github.com/Textualize/rich)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
定期タスク（Claude デスクトップアプリの Scheduled tasks）を確認待ちで止まらずに
最後まで動かすための設定を一括で行うスクリプト。

やること:
  1. ~/.claude/scheduled-tasks/*/SKILL.md から定期タスクを一覧化（指示文・実行時刻）
  2. ~/.claude/projects/**/*.jsonl から定期タスク実行の履歴を調べ、使われたツール／
     許可待ちで止まった・拒否されたツールを洗い出す
  3. ~/.claude/settings.json の permissions.allow に追記（既存設定は保持、事前に
     settings.json.bak を作成）。削除・メール送信・応募送信・git push --force は
     allow に入れず permissions.ask に入れて確認を残す
  4. 各 SKILL.md の末尾に「途中で質問せず最後まで完了させる」指示文を追記
  最後に変更内容を scheduled_tasks_report.md に書き出す。

使い方:
  python scheduled_tasks_autorun.py            # 何も変更せず、実行内容だけ表示（確認用）
  python scheduled_tasks_autorun.py --apply    # 実際に変更する
オプション:
  --claude-dir PATH   .claude フォルダの場所（既定: CLAUDE_CONFIG_DIR または ~/.claude）
  --all-sessions      定期タスクと判定できなかったセッションの履歴も集計に含める
"""

import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import sys
from collections import defaultdict
from pathlib import Path

APPEND_MARKER = "途中でユーザーに質問せず、自分で判断できることは判断して最後まで完了させること。"
APPEND_TEXT = (
    "途中でユーザーに質問せず、自分で判断できることは判断して最後まで完了させること。\n"
    "どうしても判断できないことや、送信・削除など取り消せない操作は実行せずに保留し、\n"
    "最後に『完了したこと／保留したこと／理由』をまとめて報告すること。\n"
)

# 許可なしで動くツール（allow に入れる必要がない）
NO_PERMISSION_TOOLS = {
    "Read", "Glob", "Grep", "LS", "TodoWrite", "TodoRead", "Task", "Agent",
    "TaskCreate", "TaskGet", "TaskList", "TaskUpdate", "TaskStop", "TaskOutput",
    "ToolSearch", "NotebookRead", "ExitPlanMode", "EnterPlanMode", "AskUserQuestion",
    "SendUserFile", "ListAgents", "SendMessage", "BashOutput", "KillShell", "KillBash",
    "Monitor", "ScheduleWakeup",
}
# ツール名だけで丸ごと許可するツール
WHOLE_TOOL_ALLOW = {"Edit", "Write", "MultiEdit", "NotebookEdit", "WebSearch", "WebFetch", "Skill"}
SHELL_TOOLS = {"Bash", "PowerShell"}

# 取り消せない操作とみなす MCP ツール名（server 部分を除いたツール名に対する正規表現）
DANGEROUS_MCP_RE = re.compile(
    r"(delete|trash|remove|purge|destroy|drop|erase|send|forward|reply|submit|share_file|"
    r"^apply|_apply$|apply_(job|proposal|project))",
    re.I,
)
# Gmail / Google Drive で「確認を残す」ツール（サーバー名の表記揺れに対応して展開する）
GMAIL_ASK = ["send_message", "reply", "forward", "trash_message", "trash_thread",
             "delete_draft", "delete_label", "send_draft"]
DRIVE_ASK = ["trash_file", "delete_file", "share_file"]
# Gmail / Google Drive の安全な（読み取り・下書き・ラベル整理）ツール
GMAIL_SAFE = ["search_threads", "get_thread", "get_message", "list_labels", "list_drafts",
              "get_draft", "create_draft", "update_draft", "create_label", "update_label",
              "label_message", "label_thread", "unlabel_message", "unlabel_thread",
              "update_message_labels", "apply_sensitive_message_label",
              "apply_sensitive_thread_label", "mark_message_spam", "mark_thread_spam",
              "unmark_message_spam", "unmark_thread_spam", "untrash_message", "untrash_thread"]
DRIVE_SAFE = ["search_files", "list_recent_files", "get_file_metadata", "get_file_permissions",
              "read_file_content", "download_file_content", "create_file", "update_file",
              "copy_file"]
DEFAULT_GMAIL_SERVERS = ["mcp__Gmail", "mcp__claude_ai_Gmail"]
DEFAULT_DRIVE_SERVERS = ["mcp__Google_Drive", "mcp__claude_ai_Google_Drive"]

# 確認を残すシェルコマンド
SHELL_ASK_PATTERNS = [
    "rm", "rmdir", "del", "erase", "rd", "unlink", "shred", "Remove-Item", "ri",
    "git rm", "git clean", "gh repo delete", "gh release delete",
]
FORCE_PUSH_ASK = [
    "git push --force", "git push --force *", "git push -f", "git push -f *",
    "git push * --force", "git push * --force *", "git push * -f", "git push * -f *",
    "git push --force-with-lease", "git push --force-with-lease *",
    "git push * --force-with-lease", "git push * --force-with-lease *",
]
DANGEROUS_SHELL_WORDS = {"rm", "rmdir", "del", "erase", "rd", "unlink", "shred",
                         "remove-item", "ri", "format", "mkfs"}
APPLY_SITE_RE = re.compile(r"(crowdworks|lancers)", re.I)
APPLY_ACTION_RE = re.compile(r"(apply|submit|propos|entry|応募|送信)", re.I)
DELETE_HTTP_RE = re.compile(r"(-X\s*DELETE|--request\s+DELETE|-Method\s+Delete)", re.I)

# サブコマンドまで含めてルールにするコマンド
TWO_WORD_CMDS = {"git", "npm", "npx", "pnpm", "yarn", "gh", "docker", "pip", "pip3", "uv",
                 "cargo", "go", "dotnet", "winget", "choco", "code", "claude", "gcloud",
                 "az", "aws", "kubectl", "wp", "brew", "apt", "apt-get", "conda", "poetry"}
SKIP_WORDS = {"cd", "set", "export", "echo", "true", "false", "sleep", "Start-Sleep",
              "Set-Location", "pushd", "popd", "then", "do", "done", "fi", "else", "{", "}", "(", ")"}

DENIED_RE = re.compile(
    r"(doesn't want to proceed|don't want to proceed|was rejected|user rejected|"
    r"permission to use .* (has been|was) denied|requires approval|not allowed|"
    r"Permission denied by|blocked by permission|interrupted by user|"
    r"ユーザーが拒否|許可されていません)",
    re.I,
)


def now_stamp():
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


# ---------------------------------------------------------------- 1. タスク一覧
def parse_skill_md(path: Path):
    text = path.read_text(encoding="utf-8", errors="replace")
    meta, body = {}, text
    m = re.match(r"^\ufeff?---\s*\r?\n(.*?)\r?\n---\s*\r?\n?(.*)$", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip('"').strip("'")
        body = m.group(2)
    return meta, body, text


def find_schedules(task_names, search_roots):
    """デスクトップアプリの設定 JSON から実行時刻らしき値を探す（見つからなければ空）。"""
    found = defaultdict(list)
    keys_re = re.compile(r"(cron|schedule|frequency|interval|time|hour|minute|weekday|days|nextRun|next_run)", re.I)

    def walk(obj, trail):
        if isinstance(obj, dict):
            values = [v for v in obj.values() if isinstance(v, str)]
            for name in task_names:
                if name in values or any(isinstance(v, str) and v.endswith(f"scheduled-tasks/{name}") for v in values):
                    info = {k: v for k, v in obj.items() if keys_re.search(k) and not isinstance(v, (dict, list))}
                    for k, v in obj.items():
                        if keys_re.search(k) and isinstance(v, (dict, list)):
                            info[k] = json.dumps(v, ensure_ascii=False)[:200]
                    if info:
                        found[name].append(info)
            for k, v in obj.items():
                walk(v, trail + [k])
        elif isinstance(obj, list):
            for v in obj:
                walk(v, trail)

    for root in search_roots:
        if not root.exists():
            continue
        for p in root.rglob("*.json"):
            try:
                if p.stat().st_size > 5_000_000 or "node_modules" in p.parts:
                    continue
                walk(json.loads(p.read_text(encoding="utf-8", errors="replace")), [])
            except Exception:
                continue
    return found


def list_tasks(claude_dir: Path):
    tasks = []
    base = claude_dir / "scheduled-tasks"
    if not base.exists():
        return tasks
    for d in sorted(base.iterdir()):
        f = d / "SKILL.md"
        if d.is_dir() and f.exists():
            meta, body, raw = parse_skill_md(f)
            tasks.append({"dir": d.name, "path": f, "name": meta.get("name", d.name),
                          "description": meta.get("description", ""), "body": body, "raw": raw})
    roots = [claude_dir]
    for env in ("APPDATA", "LOCALAPPDATA"):
        if os.environ.get(env):
            roots.append(Path(os.environ[env]) / "Claude")
    roots.append(Path.home() / "Library" / "Application Support" / "Claude")
    roots.append(Path.home() / ".config" / "Claude")
    sched = find_schedules([t["dir"] for t in tasks] + [t["name"] for t in tasks], roots)
    for t in tasks:
        uniq = []
        for s in sched.get(t["dir"], []) + sched.get(t["name"], []):
            if s not in uniq:
                uniq.append(s)
        t["schedule"] = uniq
    return tasks


# ---------------------------------------------------------------- 2. 履歴調査
def iter_jsonl(path: Path):
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except Exception:
                        continue
    except Exception:
        return


def first_user_text(records):
    for r in records:
        if r.get("type") != "user":
            continue
        c = (r.get("message") or {}).get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            texts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
            if texts:
                return "\n".join(texts)
    return ""


def norm(s):
    return re.sub(r"\s+", "", s or "")


def match_task(records, first_text, tasks):
    """このセッションがどの定期タスクの実行か判定する。判定できなければ None。"""
    ft = norm(first_text)
    for t in tasks:
        head = norm(t["body"])[:60]
        if head and head in ft:
            return t["dir"]
        if f"scheduled-tasks/{t['dir']}" in first_text.replace("\\", "/"):
            return t["dir"]
    for r in records[:20]:
        for k in ("origin", "promptSource", "entrypoint", "turnOrigin"):
            v = r.get(k)
            if isinstance(v, (str, dict)) and "schedul" in json.dumps(v).lower():
                return "(定期タスク・名称不明)"
    return None


def result_text(block):
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c if isinstance(b, dict))
    return ""


def scan_history(claude_dir: Path, tasks, all_sessions=False):
    projects = claude_dir / "projects"
    uses = []  # dict(tool, input, task, denied, session)
    sessions = {}
    if not projects.exists():
        return uses, sessions
    files = sorted(projects.rglob("*.jsonl"))
    main_files = [f for f in files if "subagents" not in f.parts]
    session_task = {}
    for f in main_files:
        recs = list(iter_jsonl(f))
        task = match_task(recs, first_user_text(recs), tasks)
        session_task[f.stem] = task
        sessions[f.stem] = {"file": str(f), "task": task}
    for f in files:
        sid = f.stem if "subagents" not in f.parts else f.parent.parent.name
        task = session_task.get(sid)
        if task is None and not all_sessions:
            continue
        recs = list(iter_jsonl(f))
        pending = {}
        for r in recs:
            c = (r.get("message") or {}).get("content")
            if not isinstance(c, list):
                continue
            for b in c:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    u = {"tool": b.get("name", ""), "input": b.get("input") or {},
                         "task": task or "(定期タスク以外)", "denied": False, "session": sid}
                    uses.append(u)
                    pending[b.get("id")] = u
                elif b.get("type") == "tool_result" and b.get("tool_use_id") in pending:
                    if b.get("is_error") and DENIED_RE.search(result_text(b)):
                        pending[b["tool_use_id"]]["denied"] = True
    return uses, sessions


# ---------------------------------------------------------------- 3. ルール生成
def split_shell(cmd: str):
    parts = re.split(r"\s*(?:&&|\|\||;|\||\r?\n)\s*", cmd)
    return [p.strip() for p in parts if p.strip()]


def shell_prefix(segment: str):
    try:
        toks = shlex.split(segment, posix=True)
    except ValueError:
        toks = segment.split()
    while toks and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]):  # FOO=bar cmd
        toks = toks[1:]
    if toks and toks[0] in ("sudo", "time", "nohup", "&", "."):
        toks = toks[1:]
    if not toks or toks[0] in SKIP_WORDS or toks[0].startswith("#"):
        return None
    cmd = toks[0]
    if cmd in TWO_WORD_CMDS and len(toks) > 1 and not toks[1].startswith("-"):
        return f"{cmd} {toks[1]}"
    if cmd in ("python", "python3", "py") and len(toks) > 2 and toks[1] == "-m":
        return f"{cmd} -m {toks[2]}"
    return cmd


def shell_is_dangerous(segment: str, prefix: str):
    first = prefix.split()[0].lower()
    if first in DANGEROUS_SHELL_WORDS:
        return "削除"
    if prefix in ("git rm", "git clean", "gh repo", "gh release") and re.search(r"\b(rm|clean|delete)\b", segment):
        return "削除"
    if prefix == "git push" and re.search(r"(\s-f\b|--force)", segment):
        return "git push --force"
    if DELETE_HTTP_RE.search(segment):
        return "削除（HTTP DELETE）"
    if APPLY_SITE_RE.search(segment) and APPLY_ACTION_RE.search(segment):
        return "クラウドワークス／ランサーズ応募"
    if re.search(r"(send[_-]?mail|smtp|Send-MailMessage)", segment, re.I):
        return "メール送信"
    return None


def build_rules(uses):
    allow, ask, held = [], [], []
    seen_servers = set()

    def add(lst, rule):
        if rule not in lst:
            lst.append(rule)

    for u in uses:
        tool, inp = u["tool"], u["input"]
        if tool.startswith("mcp__"):
            parts = tool.split("__")
            server = "__".join(parts[:2])
            seen_servers.add(server)
            short = parts[-1]
            if short not in GMAIL_SAFE + DRIVE_SAFE and DANGEROUS_MCP_RE.search(short):
                add(ask, tool)
                held.append((tool, "取り消せない操作（削除・送信・共有など）"))
            else:
                add(allow, tool)
        elif tool in SHELL_TOOLS:
            for seg in split_shell(str(inp.get("command", ""))):
                pre = shell_prefix(seg)
                if not pre:
                    continue
                why = shell_is_dangerous(seg, pre)
                if why:
                    held.append((f"{tool}: {seg[:120]}", why))
                    if why == "クラウドワークス／ランサーズ応募":
                        add(ask, f"{tool}({seg})")
                    continue
                add(allow, f"{tool}({pre})")
                add(allow, f"{tool}({pre} *)")
        elif tool in WHOLE_TOOL_ALLOW:
            add(allow, tool)
        elif tool in NO_PERMISSION_TOOLS or not tool:
            continue
        else:
            add(allow, tool)

    # Gmail / Drive: 実際のサーバー名 + 既定のサーバー名の両方に安全ツールを許可、危険ツールは ask
    gmail = {s for s in seen_servers if "gmail" in s.lower()} | set(DEFAULT_GMAIL_SERVERS)
    drive = {s for s in seen_servers if "drive" in s.lower()} | set(DEFAULT_DRIVE_SERVERS)
    for s in sorted(gmail):
        for t in GMAIL_SAFE:
            add(allow, f"{s}__{t}")
        for t in GMAIL_ASK:
            add(ask, f"{s}__{t}")
    for s in sorted(drive):
        for t in DRIVE_SAFE:
            add(allow, f"{s}__{t}")
        for t in DRIVE_ASK:
            add(ask, f"{s}__{t}")
    for tool in ("Bash", "PowerShell"):
        for p in SHELL_ASK_PATTERNS:
            add(ask, f"{tool}({p})")
            add(ask, f"{tool}({p} *)")
        for p in FORCE_PUSH_ASK:
            add(ask, f"{tool}({p})")
        for site in ("crowdworks", "lancers"):
            for act in ("apply", "submit", "応募"):
                add(ask, f"{tool}(*{site}*{act}*)")
        add(ask, f"{tool}(* -X DELETE *)")
    # ask と同じものは allow から外す
    allow = [r for r in allow if r not in ask]
    return allow, ask, held


def merge_settings(settings_path: Path, allow, ask, apply):
    if settings_path.exists():
        raw = settings_path.read_text(encoding="utf-8-sig")
        settings = json.loads(raw) if raw.strip() else {}
    else:
        settings = {}
    perms = settings.setdefault("permissions", {})
    cur_allow = perms.setdefault("allow", [])
    cur_ask = perms.setdefault("ask", [])
    cur_deny = perms.get("deny", [])
    added_allow = [r for r in allow if r not in cur_allow and r not in cur_deny]
    added_ask = [r for r in ask if r not in cur_ask]
    # 既存 allow に入っている危険ルール（ask で上書きされるもの）
    overridden = [r for r in cur_allow if r in ask]
    cur_allow.extend(added_allow)
    cur_ask.extend(added_ask)
    backup = None
    if apply and (added_allow or added_ask):
        if settings_path.exists():
            backup = settings_path.with_name("settings.json.bak")
            if backup.exists():
                backup.rename(settings_path.with_name(f"settings.json.bak.{now_stamp()}"))
            shutil.copy2(settings_path, backup)
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = settings_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        json.loads(tmp.read_text(encoding="utf-8"))  # 書いた JSON が壊れていないか確認
        os.replace(tmp, settings_path)
    return added_allow, added_ask, overridden, backup


# ---------------------------------------------------------------- 4. 指示文追記
def append_instructions(tasks, apply):
    changed = []
    for t in tasks:
        if APPEND_MARKER in t["raw"]:
            continue
        changed.append(t["dir"])
        if apply:
            p = t["path"]
            shutil.copy2(p, p.with_name("SKILL.md.bak"))
            sep = "" if t["raw"].endswith("\n") else "\n"
            with p.open("a", encoding="utf-8", newline="") as fh:
                fh.write(sep + "\n" + APPEND_TEXT)
    return changed


# ---------------------------------------------------------------- レポート
def md_cell(s, n=None):
    s = (s or "").replace("|", "\\|").replace("\r", "").replace("\n", "<br>")
    return s if n is None or len(s) <= n else s[:n] + "…"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="実際に変更する（指定しなければ確認のみ）")
    ap.add_argument("--claude-dir", default=os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude"))
    ap.add_argument("--all-sessions", action="store_true", help="定期タスク以外の履歴も集計に含める")
    ap.add_argument("--report", default="scheduled_tasks_report.md")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    claude_dir = Path(args.claude_dir).expanduser()
    tasks = list_tasks(claude_dir)
    uses, sessions = scan_history(claude_dir, tasks, args.all_sessions)
    allow, ask, held = build_rules(uses)
    added_allow, added_ask, overridden, backup = merge_settings(claude_dir / "settings.json", allow, ask, args.apply)
    appended = append_instructions(tasks, args.apply)

    L = []
    L.append(f"# 定期タスク自動実行設定レポート（{dt.datetime.now():%Y-%m-%d %H:%M}）\n")
    L.append(f"- 対象フォルダ: `{claude_dir}`")
    L.append(f"- モード: {'変更を適用しました' if args.apply else '確認のみ（--apply を付けると適用）'}\n")

    L.append("## 1. 定期タスク一覧\n")
    if not tasks:
        L.append(f"`{claude_dir / 'scheduled-tasks'}` に定期タスクが見つかりませんでした。\n")
    else:
        L.append("| # | タスク名 | 説明 | 実行時刻 | 指示文 |")
        L.append("|---|---|---|---|---|")
        for i, t in enumerate(tasks, 1):
            sched = "; ".join(", ".join(f"{k}={v}" for k, v in s.items()) for s in t["schedule"]) \
                or "アプリの設定画面で確認（SKILL.md には保存されていません）"
            L.append(f"| {i} | {md_cell(t['name'])} | {md_cell(t['description'], 80)} | {md_cell(sched, 120)} | {md_cell(t['body'].strip(), 400)} |")
        L.append("")

    L.append("## 2. 定期タスク実行履歴で使われたツール\n")
    matched = [s for s in sessions.values() if s["task"]]
    L.append(f"- 調べた履歴: {len(sessions)} セッション（うち定期タスク実行と判定: {len(matched)}）")
    if not matched and not args.all_sessions:
        L.append("- 定期タスクの実行履歴を判定できませんでした。`--all-sessions` を付けると全履歴から集計します。")
    agg = defaultdict(lambda: {"count": 0, "denied": 0, "tasks": set()})
    for u in uses:
        key = u["tool"]
        if u["tool"] in SHELL_TOOLS:
            pres = [shell_prefix(s) for s in split_shell(str(u["input"].get("command", "")))]
            key = f"{u['tool']}: " + ", ".join(p for p in pres if p) if any(pres) else u["tool"]
        a = agg[key]
        a["count"] += 1
        a["denied"] += int(u["denied"])
        a["tasks"].add(u["task"])
    if agg:
        L.append("\n| ツール | 回数 | 拒否・停止 | タスク |")
        L.append("|---|---|---|---|")
        for k, a in sorted(agg.items(), key=lambda kv: (-kv[1]["denied"], -kv[1]["count"])):
            L.append(f"| {md_cell(k, 100)} | {a['count']} | {a['denied']} | {md_cell(', '.join(sorted(a['tasks'])), 80)} |")
    L.append("")

    L.append("## 3. settings.json の変更\n")
    if backup:
        L.append(f"- バックアップ: `{backup}`")
    L.append(f"- permissions.allow に追加: {len(added_allow)} 件")
    for r in added_allow:
        L.append(f"  - `{r}`")
    L.append(f"- permissions.ask に追加（確認を残す操作）: {len(added_ask)} 件")
    for r in added_ask:
        L.append(f"  - `{r}`")
    if overridden:
        L.append("- 既存の allow にあったが ask が優先され、今後は確認が出るもの:")
        for r in overridden:
            L.append(f"  - `{r}`")
    if held:
        L.append("- 履歴にあったが allow に入れなかった操作:")
        for what, why in held:
            L.append(f"  - {md_cell(what)} … {why}")
    L.append("")

    L.append("## 4. 指示文の追記\n")
    if appended:
        for d in appended:
            L.append(f"- `{d}/SKILL.md` に追記" + ("（元ファイルは SKILL.md.bak）" if args.apply else "（予定）"))
    else:
        L.append("- 追記が必要なタスクはありません（すでに追記済み、またはタスクなし）")
    L.append("")

    report = "\n".join(L) + "\n"
    print(report)
    Path(args.report).write_text(report, encoding="utf-8")
    print(f"レポートを {Path(args.report).resolve()} に保存しました。")


if __name__ == "__main__":
    main()

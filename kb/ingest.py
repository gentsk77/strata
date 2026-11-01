"""kb ingest: 把 watched repo 的新增 md 原文复印进 inbox/。契约见 docs/kb-tool.md §3。

一句话：ingest 是复印机，不产生知识。它去每个 watched repo 问「上次我看过之后
你多了什么」，把新增和改动的 md **原文照抄**进 inbox/，一个字不改。读复印件、
写笔记是 /kb-distill 的活。

行为钉死（计划 2026-08-12-complete-the-kb-component.md 阶段 C）：

  - 游标住 `.kb/`（gitignored，可删）。**删游标 = 全量重捞**，这也是 inbox
    内容在蒸馏前意外丢失的恢复路径。
  - 游标只在**整批复制全部成功后**前进；复制失败（IO、权限）游标不动。
  - **跳过不算失败**：非 UTF-8 文件跳过并列出，不阻止游标前进。
  - 历史重写（游标 commit 不再是 HEAD 的祖先）：重置游标全量重扫。
  - **去重键是 repo + 相对路径**（体现在 inbox 文件名上），不是 uri，uri 内嵌
    commit hash，历史重写后全变。hash 保留在条目内部作溯源。同键覆盖，幂等。
  - rename 当 add；删除忽略（只捞新增与改动，不追踪删除）。
  - 排除：`.git/`、`.obsidian/`、`node_modules/`、一切以点开头的路径段
    （隐藏目录与隐藏文件都算）。
"""

import json
import os
import subprocess
from pathlib import Path

EXCLUDED_PARTS = {".git", ".obsidian", "node_modules"}


def _git(repo, *args, binary=False):
    kw = {"cwd": str(repo), "capture_output": True, "timeout": 60}
    if not binary:
        kw["text"] = True
        kw["encoding"] = "utf-8"
        kw["errors"] = "replace"
    # core.quotepath=false：git 默认把非 ASCII 路径转成八进制转义（"\344..."），
    # `.md` 后缀过滤会认不出中文文件名，文件被静默漏捞，对一个用中文写作的
    # 库这是复印机的主用例，必须拿到原始 UTF-8 路径。
    cmd = ["git", "-c", "core.quotepath=false"] + list(args)
    try:
        return subprocess.run(cmd, **kw)
    except (OSError, subprocess.SubprocessError) as e:
        # git 不存在或超时不许变成裸 traceback：包成失败的返回值，
        # 调用方统一走 returncode 检查，与 doctor / frontier 的姿态一致。
        out = b"" if binary else ""
        return subprocess.CompletedProcess(cmd, 127, out, str(e))


def _excluded(rel_path):
    parts = Path(rel_path).parts
    return any(p in EXCLUDED_PARTS or p.startswith(".") for p in parts)


def _entry_name(repo_key, rel_path):
    """去重键 repo+路径 → inbox 文件名。同一个源文件永远落到同一个复印件上。

    已知模糊性（单 watched repo 下无害，扩清单时要记得）：路径分隔符压平成
    `--`，所以 `a/b.md` 与字面名 `a--b.md` 会撞名；两个同名目录的 repo 也会
    共享游标键（键是 repo 目录名）。
    """
    flat = str(rel_path).replace("\\", "/").replace("/", "--")
    return "%s--%s" % (repo_key, flat)


def _load_cursor(cursor_file):
    try:
        data = json.loads(Path(cursor_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    # 合法 JSON 但不是字典（例如手滑写成列表）等于没有游标，全量重捞，
    # 不许带着错误类型往下走炸出 traceback。
    return data if isinstance(data, dict) else {}


def _save_cursor(cursor_file, data):
    cursor_file = Path(cursor_file)
    cursor_file.parent.mkdir(parents=True, exist_ok=True)
    cursor_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _changed_files(repo, prev, head):
    """(md 文件清单, 是否发生了重置)。prev 不是 head 的祖先 = 历史被重写，全量重扫。"""
    reset = False
    if prev:
        anc = _git(repo, "merge-base", "--is-ancestor", prev, head)
        if anc.returncode != 0:
            reset, prev = True, None
    if prev:
        out = _git(repo, "diff", "--name-only", "--no-renames",
                   "--diff-filter=AM", prev, head, "--", "*.md")
    else:
        # 清单必须与内容取自同一个快照（HEAD 的树）。ls-files 列的是 index，
        # 一个 staged 未提交的文件会在 git show HEAD:… 时失败，把游标永久
        # 冻住，正是「跳过不算失败」要防的那类事故。
        out = _git(repo, "ls-tree", "-r", "--name-only", head)
    if out.returncode != 0:
        raise RuntimeError("git 列文件失败：%s" % out.stderr.strip())
    files = [f.strip() for f in out.stdout.splitlines()
             if f.strip() and f.strip().endswith(".md")]
    return [f for f in files if not _excluded(f)], reset


def run(repos, inbox_dir, cursor_file):
    """对每个 watched repo 跑一遍复印。纯机械，跑一百次结果一样。"""
    inbox_dir = Path(inbox_dir)
    cursor = _load_cursor(cursor_file)
    report = {"repos": [], "copied": 0, "skipped": 0}

    for repo in repos:
        repo = Path(repo)
        key = repo.name
        entry = {"repo": key, "path": str(repo), "copied": [], "skipped_non_utf8": [],
                 "reset": False, "unchanged": False, "error": None}
        report["repos"].append(entry)

        head_out = _git(repo, "rev-parse", "HEAD")
        if head_out.returncode != 0:
            entry["error"] = "不是 git repo 或没有 commit：%s" % repo
            continue
        head = head_out.stdout.strip()
        top = _git(repo, "rev-parse", "--show-toplevel")
        if top.returncode != 0 or os.path.normcase(
                str(Path(top.stdout.strip()).resolve())) != os.path.normcase(str(repo.resolve())):
            # ls-tree 清单与 git show 的路径都以 repo 根为基准，watched 指到
            # 子目录会让两者错位，与其静默复印错东西不如明说。
            entry["error"] = "watched 必须是 repo 根目录，不能是子目录：%s" % repo
            continue
        prev = cursor.get(key)
        if prev == head:
            entry["unchanged"] = True
            continue

        try:
            files, reset = _changed_files(repo, prev, head)
        except RuntimeError as e:
            entry["error"] = str(e)
            continue
        entry["reset"] = reset

        ok_all = True
        for rel in files:
            show = _git(repo, "show", "%s:%s" % (head, rel), binary=True)
            if show.returncode != 0:
                entry["error"] = "git show 失败：%s" % rel
                ok_all = False
                break
            try:
                content = show.stdout.decode("utf-8")
            except UnicodeDecodeError:
                # 跳过不算失败：一个坏文件不许把游标永久冻住。
                entry["skipped_non_utf8"].append(rel)
                continue
            date_out = _git(repo, "log", "-1", "--format=%cs", head, "--", rel)
            event_date = date_out.stdout.strip() if date_out.returncode == 0 else ""
            uri = "%s@%s %s" % (key, head[:7], rel.replace("\\", "/"))
            body = "uri: %s\nevent_date: %s\n---\n%s" % (uri, event_date, content)
            try:
                inbox_dir.mkdir(parents=True, exist_ok=True)
                (inbox_dir / _entry_name(key, rel)).write_text(body, encoding="utf-8")
            except OSError as e:
                entry["error"] = "写 inbox 失败：%s（游标不前进，重跑即续）" % e
                ok_all = False
                break
            entry["copied"].append(rel)

        if ok_all:
            cursor[key] = head
            _save_cursor(cursor_file, cursor)
        report["copied"] += len(entry["copied"])
        report["skipped"] += len(entry["skipped_non_utf8"])

    return report


def render(report):
    lines = []
    for r in report["repos"]:
        if r["error"]:
            lines.append("%s: 失败：%s" % (r["repo"], r["error"]))
            continue
        if r["unchanged"]:
            lines.append("%s: 无新增（游标已在 HEAD）" % r["repo"])
            continue
        head_note = "（游标失效，全量重扫）" if r["reset"] else ""
        lines.append("%s: 复印 %d 条%s" % (r["repo"], len(r["copied"]), head_note))
        for rel in r["copied"][:20]:
            lines.append("  + %s" % rel)
        if len(r["copied"]) > 20:
            lines.append("  …共 %d 条" % len(r["copied"]))
        for rel in r["skipped_non_utf8"]:
            lines.append("  ~ 跳过（非 UTF-8）：%s" % rel)
    lines.append("合计复印 %d，跳过 %d" % (report["copied"], report["skipped"]))
    return "\n".join(lines)

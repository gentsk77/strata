"""doctor 的 workspace 解析回归。零依赖：python tests/test_doctor_workspace.py

要守住的契约：artifact 指针相对「strata 所在的 workspace 目录」解析，
主 checkout 和 git worktree 必须解析到**同一个** workspace，2026-08-12 修复前，
worktree 里解析到 .claude/worktrees/，52 条指针整批误报。
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from kb.doctor import _workspace_root, check

# --------------------------------------------------------------------------
# 极简断言框架（与 test_hubs.py 同款）
# --------------------------------------------------------------------------

_checks = 0
_failures = []
_current = ""


def _record(msg):
    frame = sys._getframe(2)
    _failures.append(
        "  [%s] %s:%d\n    %s" % (_current, Path(frame.f_code.co_filename).name, frame.f_lineno, msg)
    )


def ok(cond, what):
    global _checks
    _checks += 1
    if not cond:
        _record(what)


def eq(actual, expected, what):
    global _checks
    _checks += 1
    if actual != expected:
        _record("%s\n      期望: %r\n      实际: %r" % (what, expected, actual))


_TESTS = []


def test(fn):
    _TESTS.append(fn)
    return fn


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t"] + list(args),
        cwd=str(cwd), capture_output=True, text=True, timeout=30,
    )


def _in_a_git_repo(path):
    out = git(path, "rev-parse", "--git-common-dir")
    return out.returncode == 0


# --------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------


@test
def test_bare_directory_falls_back_to_two_levels_up():
    """不在任何 git repo 里时，行为与修复前一致：notes/ 的上两级。"""
    tmp = Path(tempfile.mkdtemp(prefix="kb-ws-test-"))
    try:
        if _in_a_git_repo(tmp):
            # 临时目录竟然在某个 repo 里（不该发生），前提不成立，跳过而不假红。
            ok(True, "前提不成立，跳过")
            return
        notes = tmp / "repo" / "notes"
        notes.mkdir(parents=True)
        eq(_workspace_root(notes), tmp.resolve(), "裸目录应回退到 notes/ 上两级")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@test
def test_worktree_resolves_same_workspace_as_main_checkout():
    """同一个 repo，主 checkout 和 worktree 里的 notes/ 必须给出同一个 workspace。"""
    if git(Path.cwd(), "--version").returncode != 0:
        ok(True, "机器上没有 git，跳过")
        return
    tmp = Path(tempfile.mkdtemp(prefix="kb-ws-test-"))
    repo = tmp / "ws" / "strata-like"
    wt = repo / ".claude" / "worktrees" / "wt"
    note = (
        "---\n"
        "id: probe\n"
        "title: 带 artifact 的探针笔记\n"
        "evidence: stated\n"
        "status: settled\n"
        "event_date: 2026-01-01\n"
        "source: t\n"
        "artifacts:\n"
        "  - toolrepo/tool.md\n"
        "---\n\n正文。\n"
    )
    try:
        (repo / "notes").mkdir(parents=True)
        (repo / "notes" / "probe.md").write_text(note, encoding="utf-8")
        tool = tmp / "ws" / "toolrepo"
        tool.mkdir(parents=True)
        (tool / "tool.md").write_text("活工具。\n", encoding="utf-8")
        for step in (("init",), ("add", "-A"), ("commit", "-m", "x")):
            out = git(repo, *step)
            if out.returncode != 0:
                ok(False, "git %s 失败：%s" % (step[0], out.stderr.strip()))
                return
        out = git(repo, "worktree", "add", str(wt))
        if out.returncode != 0:
            ok(False, "git worktree add 失败：%s" % out.stderr.strip())
            return

        expected = (tmp / "ws").resolve()
        eq(_workspace_root(repo / "notes"), expected, "主 checkout 的 workspace 不对")
        eq(_workspace_root(wt / "notes"), expected,
           "worktree 的 workspace 必须与主 checkout 一致（修复前落在 .claude/worktrees/）")

        # 端到端闭环：worktree 里对带 artifact 指针的笔记跑 doctor，零误报；
        # 指针真断掉时，两边报得一样。
        eq(check(wt / "notes")["stale_artifacts"], [],
           "worktree 内 doctor 对存在的 artifact 不该误报")
        (tool / "tool.md").unlink()
        eq(check(wt / "notes")["stale_artifacts"],
           check(repo / "notes")["stale_artifacts"],
           "指针真断掉时，worktree 与主 checkout 的报告必须一致")
    finally:
        git(repo, "worktree", "remove", "--force", str(wt))
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------


def main():
    global _current
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    for fn in _TESTS:
        _current = fn.__name__
        before = len(_failures)
        try:
            fn()
        except Exception as e:
            import traceback
            _failures.append("  [%s] 用例抛异常：%s\n%s" % (
                _current, e, "".join("    " + l for l in traceback.format_exc().splitlines(True))))
        mark = "ok  " if len(_failures) == before else "FAIL"
        print("%s %s" % (mark, fn.__name__))

    print("\n%d 条断言，%d 个用例" % (_checks, len(_TESTS)))
    if _failures:
        print("\n失败 %d 处：\n" % len(_failures))
        for f in _failures:
            print(f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

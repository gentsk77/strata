"""kb ingest 的回归测试。零依赖：python tests/test_ingest.py

watched repo 在临时目录里现造（git init + commit），跑完删。全部断言对着
计划钉死的行为：幂等、游标语义、格式、排除清单、非 UTF-8 跳过、历史重写重置。
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from kb import ingest

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


class Scene:
    """一个 watched repo + 一个 inbox + 一个游标，全在临时目录里。"""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kb-ingest-test-"))
        self.repo = self.tmp / "watched"
        self.inbox = self.tmp / "inbox"
        self.cursor = self.tmp / ".kb" / "ingest-cursor.json"
        self.repo.mkdir(parents=True)
        git(self.repo, "init")

    def write(self, rel, text):
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def write_bytes(self, rel, data):
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def commit(self, msg="c"):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", msg)

    def run(self):
        return ingest.run([self.repo], self.inbox, self.cursor)

    def entries(self):
        if not self.inbox.is_dir():
            return []
        return sorted(p.name for p in self.inbox.iterdir())

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def scene_test(fn):
    def wrapper():
        s = Scene()
        try:
            fn(s)
        finally:
            s.cleanup()
    wrapper.__name__ = fn.__name__
    return wrapper


# --------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------


@test
@scene_test
def test_first_run_copies_and_second_run_is_zero(s):
    s.write("a.md", "甲\n")
    s.write("docs/b.md", "乙\n")
    s.write("c.txt", "非 md，不该被捞\n")
    s.commit()
    r1 = s.run()
    eq(r1["copied"], 2, "首跑该复印 2 条 md")
    eq(s.entries(), ["watched--a.md", "watched--docs--b.md"], "去重键 = repo+路径")
    r2 = s.run()
    eq(r2["copied"], 0, "验收 (a)：连跑两遍，第二遍零新增")
    ok(r2["repos"][0]["unchanged"], "第二遍该报无新增")


@test
@scene_test
def test_entry_format_uri_event_date_content(s):
    s.write("a.md", "原文第一行\n原文第二行\n")
    s.commit()
    s.run()
    body = (s.inbox / "watched--a.md").read_text(encoding="utf-8")
    lines = body.split("\n")
    ok(lines[0].startswith("uri: watched@"), "第一行是 uri，带 repo@hash：%r" % lines[0])
    ok(lines[0].endswith(" a.md"), "uri 尾部是相对路径：%r" % lines[0])
    ok(lines[1].startswith("event_date: 20"), "第二行是 event_date：%r" % lines[1])
    eq(lines[2], "---", "第三行是分隔符")
    eq("\n".join(lines[3:]), "原文第一行\n原文第二行\n", "正文原文照抄，一字不改")


@test
@scene_test
def test_excluded_dirs_and_hidden_dirs(s):
    s.write("a.md", "甲\n")
    s.write(".obsidian/conf.md", "不该被捞\n")
    s.write("node_modules/x.md", "不该被捞\n")
    s.write(".hidden/y.md", "不该被捞\n")
    s.commit()
    s.run()
    eq(s.entries(), ["watched--a.md"], "排除清单：.obsidian、node_modules、隐藏目录")


@test
@scene_test
def test_non_utf8_skipped_and_cursor_still_advances(s):
    s.write("a.md", "甲\n")
    s.write_bytes("bad.md", b"\xff\xfe\x00 not utf8 \x9c")
    s.commit()
    r1 = s.run()
    eq(r1["repos"][0]["skipped_non_utf8"], ["bad.md"], "非 UTF-8 该被跳过并列出")
    eq(s.entries(), ["watched--a.md"], "坏文件不产生条目")
    r2 = s.run()
    ok(r2["repos"][0]["unchanged"], "跳过不算失败：游标该已前进，第二遍无新增")


@test
@scene_test
def test_incremental_only_copies_changes(s):
    s.write("a.md", "甲\n")
    s.write("b.md", "乙\n")
    s.commit()
    s.run()
    (s.inbox / "watched--a.md").unlink()  # 便于确认 a 没被重复复印
    s.write("b.md", "乙改\n")
    s.commit("edit b")
    r = s.run()
    eq(r["repos"][0]["copied"], ["b.md"], "增量跑只该复印改过的 b.md")
    ok(not (s.inbox / "watched--a.md").exists(), "没动过的 a.md 不该被重新复印")
    body = (s.inbox / "watched--b.md").read_text(encoding="utf-8")
    ok("乙改" in body, "复印件该是 HEAD 版本（同键覆盖）")


@test
@scene_test
def test_rename_is_an_add(s):
    s.write("a.md", "甲\n")
    s.commit()
    s.run()
    git(s.repo, "mv", "a.md", "a2.md")
    s.commit("rename")
    r = s.run()
    eq(r["repos"][0]["copied"], ["a2.md"], "rename 当 add：新路径被复印")
    ok((s.inbox / "watched--a.md").exists(), "旧复印件保留（删除忽略）")


@test
@scene_test
def test_history_rewrite_resets_and_stays_idempotent(s):
    s.write("a.md", "甲\n")
    s.commit()
    s.run()
    git(s.repo, "commit", "--amend", "-m", "rewritten")
    r = s.run()
    ok(r["repos"][0]["reset"], "游标 commit 不再是 HEAD 祖先，该报重置")
    eq(r["repos"][0]["copied"], ["a.md"], "重置后全量重扫")
    eq(s.entries(), ["watched--a.md"], "同键覆盖，重扫不产生重复原料")


@test
@scene_test
def test_deleted_cursor_means_full_rescan(s):
    s.write("a.md", "甲\n")
    s.write("b.md", "乙\n")
    s.commit()
    s.run()
    before = {p.name: p.read_text(encoding="utf-8") for p in s.inbox.iterdir()}
    s.cursor.unlink()
    r = s.run()
    eq(len(r["repos"][0]["copied"]), 2, "验收 (c)：删游标 = 全量重捞")
    after = {p.name: p.read_text(encoding="utf-8") for p in s.inbox.iterdir()}
    eq(after, before, "重捞产物与首跑一致（幂等）")


@test
@scene_test
def test_staged_but_uncommitted_file_does_not_freeze(s):
    s.write("a.md", "甲\n")
    s.commit()
    s.write("staged.md", "已 stage 未提交\n")
    git(s.repo, "add", "staged.md")
    r = s.run()
    ok(not r["repos"][0]["error"], "staged 未提交的文件不该让整批失败")
    eq(r["repos"][0]["copied"], ["a.md"], "清单来自 HEAD 快照，不含未提交文件")
    r2 = s.run()
    ok(r2["repos"][0]["unchanged"], "游标该已前进，不被 staged 文件冻住")


@test
@scene_test
def test_copy_failure_freezes_cursor_and_retry_resumes(s):
    s.write("a.md", "甲\n")
    s.commit()
    s.inbox.write_text("占位文件，让 inbox 目录建不起来", encoding="utf-8")
    r1 = s.run()
    ok(r1["repos"][0]["error"], "写 inbox 失败该报错")
    ok(not s.cursor.exists(), "复制失败游标不该前进")
    s.inbox.unlink()
    r2 = s.run()
    eq(r2["repos"][0]["copied"], ["a.md"], "重跑即续：游标没动过，全部补上")
    ok(s.cursor.exists(), "整批成功后游标才前进")


@test
@scene_test
def test_non_ascii_filenames_are_ingested(s):
    s.write("中文笔记.md", "第一版\n")
    s.commit()
    r = s.run()
    eq(r["repos"][0]["copied"], ["中文笔记.md"],
       "非 ASCII 文件名必须被捞到（git 默认 quotepath 会把它转义掉）")
    ok((s.inbox / "watched--中文笔记.md").exists(), "复印件落地")
    s.write("中文笔记.md", "第二版\n")
    s.commit("edit")
    r2 = s.run()
    eq(r2["repos"][0]["copied"], ["中文笔记.md"], "增量分支同样要认出非 ASCII 路径")
    ok("第二版" in (s.inbox / "watched--中文笔记.md").read_text(encoding="utf-8"),
       "同键覆盖为 HEAD 版本")


@test
@scene_test
def test_watched_subdirectory_is_rejected(s):
    s.write("docs/a.md", "甲\n")
    s.commit()
    r = ingest.run([s.repo / "docs"], s.inbox, s.cursor)
    ok(r["repos"][0]["error"], "watched 指到 repo 子目录该明确报错")


@test
@scene_test
def test_not_a_repo_reports_error(s):
    plain = s.tmp / "plain"
    plain.mkdir()
    r = ingest.run([plain], s.inbox, s.cursor)
    ok(r["repos"][0]["error"], "非 git 目录该报错而不是静默")


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

"""kb list 过滤器（--since / --evidence）的回归测试。零依赖：python tests/test_list.py

走 CLI 层（kb.cli.main + 临时目录）：过滤逻辑全部住在 cmd_list 里，函数层没有
独立实现可测。语料在临时目录现造现删，不进 tests/fixtures/，那底下的 notes/
是给 doctor 用的、故意写坏的夹具。
"""

import contextlib
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from kb import cli

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


# --------------------------------------------------------------------------
# 语料
# --------------------------------------------------------------------------

NOTE = """---
id: %(slug)s
title: %(title)s
evidence: %(evidence)s
status: settled
event_date: %(date)s
source: t
---

正文。
"""

ROWS = [
    ("early-revealed", "早的已验证", "revealed", "2026-08-01"),
    ("mid-stated", "中间的口头", "stated", "2026-08-04"),
    ("late-inferred", "晚的自推", "inferred", "2026-08-10"),
    ("late-revealed", "晚的已验证", "revealed", "2026-08-10"),
]


def make_corpus(root):
    notes = root / "notes"
    notes.mkdir(parents=True)
    for slug, title, ev, date in ROWS:
        (notes / ("%s.md" % slug)).write_text(
            NOTE % {"slug": slug, "title": title, "evidence": ev, "date": date},
            encoding="utf-8",
        )


def run_cli(root, *extra):
    buf, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        code = cli.main(["--root", str(root), "list"] + list(extra))
    return code, buf.getvalue(), err.getvalue()


def with_corpus(fn):
    tmp = Path(tempfile.mkdtemp(prefix="kb-list-test-"))
    try:
        make_corpus(tmp)
        fn(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------


@test
def test_no_filter_lists_everything():
    def body(root):
        code, out, _ = run_cli(root)
        eq(code, 0, "无过滤应退出 0")
        for _, title, _, _ in ROWS:
            ok(title in out, "缺 %s" % title)

    with_corpus(body)


@test
def test_since_filters_and_shows_dates():
    def body(root):
        code, out, _ = run_cli(root, "--since", "2026-08-04")
        eq(code, 0, "--since 应退出 0")
        ok("早的已验证" not in out, "2026-08-01 的笔记应被 --since 2026-08-04 滤掉")
        ok("中间的口头" in out, "含当天：2026-08-04 的笔记应保留")
        ok("晚的自推" in out and "晚的已验证" in out, "窗口内的笔记应保留")
        ok("2026-08-04" in out and "2026-08-10" in out, "--since 模式下行首应带日期")

    with_corpus(body)


@test
def test_evidence_filter():
    def body(root):
        code, out, _ = run_cli(root, "--evidence", "revealed")
        eq(code, 0, "--evidence 应退出 0")
        ok("早的已验证" in out and "晚的已验证" in out, "revealed 两条都该在")
        ok("中间的口头" not in out and "晚的自推" not in out, "其他档不该出现")

    with_corpus(body)


@test
def test_filters_stack():
    def body(root):
        code, out, _ = run_cli(root, "--since", "2026-08-05", "--evidence", "revealed")
        eq(code, 0, "叠加过滤应退出 0")
        ok("晚的已验证" in out, "同时过两个过滤器的那条该在")
        for absent in ("早的已验证", "中间的口头", "晚的自推"):
            ok(absent not in out, "%s 不该出现" % absent)

    with_corpus(body)


@test
def test_bad_since_exits_2():
    def body(root):
        code, _, err = run_cli(root, "--since", "上周")
        eq(code, 2, "非法日期应退出 2")
        ok("YYYY-MM-DD" in err, "错误信息应指出日期格式")

    with_corpus(body)


@test
def test_empty_result_says_so():
    def body(root):
        code, out, _ = run_cli(root, "--since", "2027-01-01")
        eq(code, 0, "0 命中不是错误，应退出 0")
        ok("0 条命中" in out, "空结果要明说，不能输出空白")
        ok("--since 2027-01-01" in out, "空结果要报出生效的过滤器")

    with_corpus(body)


@test
def test_json_carries_event_date():
    def body(root):
        code, out, _ = run_cli(root, "--json", "--since", "2026-08-04")
        eq(code, 0, "--json --since 应退出 0")
        payload = json.loads(out)
        slugs = {row["slug"] for rows in payload.values() for row in rows}
        eq(slugs, {"mid-stated", "late-inferred", "late-revealed"}, "JSON 过滤结果不对")
        for rows in payload.values():
            for row in rows:
                ok(row.get("event_date"), "JSON 行缺 event_date：%r" % row)

    with_corpus(body)


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

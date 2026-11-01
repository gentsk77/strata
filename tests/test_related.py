"""kb related 的回归测试。零依赖，直接跑：python tests/test_related.py

退出码 0 = 全过，1 = 有失败。框架照抄 tests/test_hubs.py。related 和 query
一样没有 MIN_NOTES 门槛，CLI 语料不用凑 40 条。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from kb import related
from kb.notes import Note

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
# 造语料的积木
# --------------------------------------------------------------------------


def mk(slug, title, body):
    return Note("notes/%s.md" % slug, {"id": slug, "title": title}, body)


def chain_corpus():
    """一条链 a → b → c → d，加一条 b↔e 互链和一条 c→e 边（e 在 2 跳处成环）。

        a -- b -- c -- d
              \\   |
               \\  |
                 e

    从 a 出发：1 跳到 b，2 跳到 c、e，3 跳到 d。
    """
    return [
        mk("aa", "a 的论断", "起点。这条延伸出 [[bb]]。"),
        mk("bb", "b 的论断", "中继。前提是 [[cc]]。它和 [[ee]] 互为反例。"),
        mk("cc", "c 的论断", "第三条。收尾指向 [[dd]]。同构见 [[ee]]。"),
        mk("dd", "d 的论断", "末端，没有出链。"),
        mk("ee", "e 的论断", "反例回指 [[bb]]。"),
        mk("lone", "孤儿", "谁也不挂。悬空的 [[ghost]] 不算数。"),
    ]


def slugs_at(result, d):
    return [n["slug"] for n in result["nodes"] if n["hops"] == d]


def edge(result, a, b):
    key = (min(a, b), max(a, b))
    for e in result["edges"]:
        if (e["a"], e["b"]) == key:
            return e
    return None


# --------------------------------------------------------------------------
# 1. BFS 与诱导子图
# --------------------------------------------------------------------------


@test
def test_one_hop_is_direct_neighbors_only():
    result = related.compute(chain_corpus(), "bb", hops=1)
    eq(slugs_at(result, 0), ["bb"], "0 跳只有起点自己")
    eq(slugs_at(result, 1), ["aa", "cc", "ee"], "1 跳是全部直接邻居（含入链方向）")
    eq(result["counts"]["nodes"], 4, "共 4 个节点")


@test
def test_hops_is_shortest_distance_no_duplicates():
    result = related.compute(chain_corpus(), "aa", hops=3)
    eq(slugs_at(result, 1), ["bb"], "1 跳只有 bb")
    eq(slugs_at(result, 2), ["cc", "ee"], "cc 和 ee 都在 2 跳")
    eq(slugs_at(result, 3), ["dd"], "dd 在 3 跳")
    all_slugs = [n["slug"] for n in result["nodes"]]
    eq(len(all_slugs), len(set(all_slugs)), "每个节点只出现一次，按最短距离记跳数")


@test
def test_induced_subgraph_includes_frontier_to_frontier_edge():
    """cc 和 ee 都在 2 跳边界上，它们之间的边也要报，是诱导子图不是 BFS 树。"""
    result = related.compute(chain_corpus(), "aa", hops=2)
    ok(edge(result, "cc", "ee") is not None, "边界节点之间的边不能丢")
    ok(edge(result, "cc", "dd") is None, "dd 在 3 跳，不在子图里，它的边不能进来")


@test
def test_hops_zero_gives_origin_only():
    result = related.compute(chain_corpus(), "bb", hops=0)
    eq([n["slug"] for n in result["nodes"]], ["bb"], "0 跳只有起点")
    eq(result["edges"], [], "没有别的节点就没有边")


@test
def test_orphan_origin_has_no_edges():
    result = related.compute(chain_corpus(), "lone", hops=2)
    eq([n["slug"] for n in result["nodes"]], ["lone"], "孤儿走几跳都只有自己")
    eq(result["edges"], [], "孤儿没有边")


@test
def test_dangling_link_creates_nothing():
    result = related.compute(chain_corpus(), "lone", hops=1)
    ok(all(n["slug"] != "ghost" for n in result["nodes"]), "悬空链接不能造出节点")


# --------------------------------------------------------------------------
# 2. 理由句
# --------------------------------------------------------------------------


@test
def test_edge_reason_is_the_sentence_holding_the_link():
    result = related.compute(chain_corpus(), "bb", hops=1)
    e = edge(result, "bb", "cc")
    eq(e["reasons"], [{"from": "bb", "sentence": "前提是 [[cc]]。"}],
       "理由句是 bb 正文里挂 [[cc]] 的那一句，方向从 bb 出发")


@test
def test_reciprocal_edge_carries_both_sides():
    result = related.compute(chain_corpus(), "bb", hops=1)
    e = edge(result, "bb", "ee")
    eq(len(e["reasons"]), 2, "互链的边两边各有一句理由")
    sides = {r["from"]: r["sentence"] for r in e["reasons"]}
    eq(sides["bb"], "它和 [[ee]] 互为反例。", "bb 侧的理由句")
    eq(sides["ee"], "反例回指 [[bb]]。", "ee 侧的理由句")


@test
def test_reason_matches_exact_slug_not_prefix():
    """挂着 [[aa-longer]] 的句子不能被错认成 aa 的理由。"""
    notes = [
        mk("xx", "x", "先提 [[aa-longer]] 一下。真正的理由在这句 [[aa]]。"),
        mk("aa", "短", "空。"),
        mk("aa-longer", "长", "空。"),
    ]
    e = edge(related.compute(notes, "xx", hops=1), "xx", "aa")
    eq(e["reasons"][0]["sentence"], "真正的理由在这句 [[aa]]。",
       "理由句必须精确匹配链接目标，不做前缀子串搜")


@test
def test_unextractable_reason_is_null_not_fabricated():
    """链接挂在没有句末标点的表格单元格里时，句子切分仍能兜住；
    真正抽不出的场景是链接不在任何句子里，用 frontmatter 之外拼不出来，
    所以这里锁定的是「reasons 里 sentence 可以为 None 而结构不崩」。"""
    notes = [
        mk("tt", "表格", "| 列 | [[uu]] |"),
        mk("uu", "目标", "空。"),
    ]
    e = edge(related.compute(notes, "tt", hops=1), "tt", "uu")
    eq(len(e["reasons"]), 1, "边还在，理由结构还在")
    r = e["reasons"][0]
    eq(r["from"], "tt", "方向还在")
    ok(r["sentence"] is None or "[[uu]]" in r["sentence"],
       "sentence 要么是真句子要么是 None，不许硬造")


# --------------------------------------------------------------------------
# 3. 错误路径与形状
# --------------------------------------------------------------------------


@test
def test_unknown_slug_raises_with_candidates():
    raised = None
    try:
        related.compute(chain_corpus(), "cc-typo", hops=1)
    except related.UnknownSlug as e:
        raised = e
    ok(raised is not None, "未知 slug 应抛 UnknownSlug")
    if raised is not None:
        eq(raised.as_json()["error"], "unknown_slug", "错误有机器可读的类型")

    try:
        related.compute(chain_corpus(), "c", hops=1)
    except related.UnknownSlug as e:
        ok("cc" in e.candidates, "候选里应有包含检索文字的 slug")


@test
def test_negative_hops_raises():
    raised = False
    try:
        related.compute(chain_corpus(), "aa", hops=-1)
    except ValueError:
        raised = True
    ok(raised, "负数 hops 应抛 ValueError")


@test
def test_result_is_json_serialisable():
    result = related.compute(chain_corpus(), "bb", hops=2)
    ok(json.loads(json.dumps(result, ensure_ascii=False)) == result, "结果应能无损过一轮 JSON")


@test
def test_compute_does_not_mutate_input():
    notes = chain_corpus()
    before = [(n.slug, n.body, list(n.links)) for n in notes]
    related.compute(notes, "aa", hops=3)
    eq([(n.slug, n.body, list(n.links)) for n in notes], before, "compute 不许改输入")


@test
def test_render_smoke():
    text = related.render(related.compute(chain_corpus(), "bb", hops=1))
    ok("从 bb 走 1 跳" in text, "抬头要报起点和跳数")
    ok("前提是 [[cc]]。" in text, "理由句要印出来")
    ok(not text.endswith("\n"), "渲染结果不该以空行结尾")
    lone = related.render(related.compute(chain_corpus(), "lone", hops=1))
    ok("孤儿" in lone or "没有边" in lone, "孤儿要有一句人话")


# --------------------------------------------------------------------------
# 4. CLI
# --------------------------------------------------------------------------

NOTE_TEMPLATE = """---
id: {slug}
title: {title}
evidence: stated
status: settled
event_date: 2026-08-01
source: tests/test_related.py 生成的临时语料
---

{body}
"""


def write_note_files(root, notes):
    notes_dir = Path(root) / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    for n in notes:
        (notes_dir / (n.slug + ".md")).write_text(
            NOTE_TEMPLATE.format(slug=n.slug, title=n.fm.get("title", n.slug), body=n.body),
            encoding="utf-8",
        )
    return str(notes_dir)


def run_kb(args):
    environ = dict(os.environ)
    environ["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "kb", *args],
        cwd=str(REPO_ROOT),
        env=environ,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


@test
def test_cli_json_works_in_both_flag_positions():
    tmp = tempfile.mkdtemp(prefix="kb-related-cli-")
    try:
        write_note_files(tmp, chain_corpus())
        payloads = []
        for argv in (["--root", tmp, "--json", "related", "bb"],
                     ["--root", tmp, "related", "bb", "--json"],
                     ["related", "bb", "--root", tmp, "--json"]):
            proc = run_kb(argv)
            eq(proc.returncode, 0, "kb %s 应退出 0（stderr: %s）" % (" ".join(argv), proc.stderr[:200]))
            parsed, why = None, ""
            try:
                parsed = json.loads(proc.stdout)
            except ValueError as e:
                why = "，json.loads 报 %s" % e
            ok(parsed is not None, "kb %s 应输出可解析的 JSON%s" % (" ".join(argv), why))
            if parsed is not None:
                eq(parsed["origin"], "bb", "origin 应透传")
                payloads.append(json.dumps(parsed, sort_keys=True, ensure_ascii=False))
        eq(len(set(payloads)), 1, "三种 flag 位置应给出完全一样的 JSON")

        proc = run_kb(["--root", tmp, "related", "bb", "--hops", "2"])
        eq(proc.returncode, 0, "人类可读模式应退出 0")
        ok("从 bb 走 2 跳" in proc.stdout, "--hops 应透传")
        ok(not proc.stdout.lstrip().startswith("{"), "无 --json 时不该吐 JSON")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@test
def test_cli_unknown_slug_exits_two_with_json_error():
    tmp = tempfile.mkdtemp(prefix="kb-related-unknown-")
    try:
        write_note_files(tmp, chain_corpus())
        proc = run_kb(["--root", tmp, "--json", "related", "no-such-slug"])
        eq(proc.returncode, 2, "未知 slug 应退出 2")
        payload = json.loads(proc.stdout)
        eq(payload["error"], "unknown_slug", "错误有机器可读的类型")

        proc = run_kb(["--root", tmp, "related", "no-such-slug"])
        eq(proc.returncode, 2, "人类可读模式同样退出 2")
        eq(proc.stdout.strip(), "", "报错时不往 stdout 写内容")
        ok("no-such-slug" in proc.stderr, "stderr 要点名是哪个 slug 不存在")
    finally:
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
        except Exception as e:  # 用例自己炸了也算失败，不能让整轮中断
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

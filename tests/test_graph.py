"""kb graph 的回归测试。零依赖，直接跑：python tests/test_graph.py

退出码 0 = 全过，1 = 有失败。框架照抄 tests/test_related.py。

这个套件盯三件事：
  1. 形状锁死，审理屏的图栏按字段名消费这份 JSON，字段改名就是接口断裂。
  2. 与 related / hubs 同一张图，三条命令共用一份邻接与理由句，
     任何一处各写一份，同一个库就会给出两张互相打架的图。
  3. 确定性，两次跑必须逐字节相同，否则 diff 全是噪声。
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

from kb import graph, hubs, related
from kb.notes import Note

# --------------------------------------------------------------------------
# 极简断言框架（与 test_related.py 同款）
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


def mk(slug, title, body, status="settled", evidence="stated"):
    return Note(
        "notes/%s.md" % slug,
        {"id": slug, "title": title, "status": status, "evidence": evidence},
        body,
    )


def corpus():
    """一条链 aa → bb → cc → dd，bb↔ee 互链，cc→ee，外加一个孤儿。

        aa -- bb -- cc -- dd
               \\    |
                \\   |
                  ee          lone（悬空指向 ghost）

    status 与 evidence 故意各不相同，用来锁「front matter 原样透传」。
    """
    return [
        mk("aa", "a 的论断", "起点。这条延伸出 [[bb]]。", "settled", "revealed"),
        mk("bb", "b 的论断", "中继。前提是 [[cc]]。它和 [[ee]] 互为反例。", "unsettled", "stated"),
        mk("cc", "c 的论断", "第三条。收尾指向 [[dd]]。同构见 [[ee]]。", "settled", "inferred"),
        mk("dd", "d 的论断", "末端，没有出链。", "settled", "stated"),
        mk("ee", "e 的论断", "反例回指 [[bb]]。", "unsettled", "revealed"),
        mk("lone", "孤儿", "谁也不挂。悬空的 [[ghost]] 不算数。", "unsettled", "stated"),
    ]


# 整份期望值写死在这里。逐字段断言看着更「精确」，实则漏字段：新增一个键
# 没有任何用例会红，而这份 JSON 是给别的程序消费的接口。
EXPECTED = {
    "nodes": [
        {"slug": "aa", "title": "a 的论断", "status": "settled", "evidence": "revealed", "degree": 1},
        {"slug": "bb", "title": "b 的论断", "status": "unsettled", "evidence": "stated", "degree": 3},
        {"slug": "cc", "title": "c 的论断", "status": "settled", "evidence": "inferred", "degree": 3},
        {"slug": "dd", "title": "d 的论断", "status": "settled", "evidence": "stated", "degree": 1},
        {"slug": "ee", "title": "e 的论断", "status": "unsettled", "evidence": "revealed", "degree": 2},
        {"slug": "lone", "title": "孤儿", "status": "unsettled", "evidence": "stated", "degree": 0},
    ],
    "edges": [
        {"source": "aa", "target": "bb", "reason": "这条延伸出 [[bb]]。", "reason_from": "aa"},
        {"source": "bb", "target": "cc", "reason": "前提是 [[cc]]。", "reason_from": "bb"},
        {"source": "bb", "target": "ee", "reason": "它和 [[ee]] 互为反例。", "reason_from": "bb"},
        {"source": "cc", "target": "dd", "reason": "收尾指向 [[dd]]。", "reason_from": "cc"},
        {"source": "cc", "target": "ee", "reason": "同构见 [[ee]]。", "reason_from": "cc"},
    ],
    "counts": {"nodes": 6, "edges": 5},
}


def edge(result, a, b):
    key = (min(a, b), max(a, b))
    for e in result["edges"]:
        if (e["source"], e["target"]) == key:
            return e
    return None


def node(result, slug):
    for n in result["nodes"]:
        if n["slug"] == slug:
            return n
    return None


# --------------------------------------------------------------------------
# 1. 形状：整份 payload 锁死
# --------------------------------------------------------------------------


@test
def test_whole_payload_matches_expected():
    eq(graph.compute(corpus()), EXPECTED, "整份 payload 必须逐字段与期望一致")


@test
def test_top_level_keys_are_exactly_three():
    eq(sorted(graph.compute(corpus())), ["counts", "edges", "nodes"],
       "顶层只有 nodes / edges / counts 三个键")


@test
def test_counts_agree_with_the_lists():
    result = graph.compute(corpus())
    eq(result["counts"]["nodes"], len(result["nodes"]), "counts.nodes 要等于 nodes 的长度")
    eq(result["counts"]["edges"], len(result["edges"]), "counts.edges 要等于 edges 的长度")


@test
def test_status_and_evidence_come_from_frontmatter():
    result = graph.compute(corpus())
    eq(node(result, "bb")["status"], "unsettled", "status 原样透传")
    eq(node(result, "cc")["evidence"], "inferred", "evidence 原样透传")
    eq(node(result, "aa")["evidence"], "revealed", "另一个证据档也透传")


@test
def test_missing_frontmatter_fields_degrade_to_empty_string():
    """缺字段不许崩，也不许编一个默认值出来。"""
    bare = [Note("notes/xx.md", {"id": "xx"}, "空。"), ]
    n = graph.compute(bare)["nodes"][0]
    eq((n["title"], n["status"], n["evidence"]), ("", "", ""), "缺字段给空串，不猜")


@test
def test_degree_is_distinct_undirected_neighbours():
    result = graph.compute(corpus())
    eq(node(result, "bb")["degree"], 3, "bb 连 aa、cc、ee")
    eq(node(result, "ee")["degree"], 2, "互链只算一条边")
    eq(node(result, "lone")["degree"], 0, "孤儿度数是 0")


@test
def test_empty_library_is_a_valid_empty_graph():
    eq(graph.compute([]), {"nodes": [], "edges": [], "counts": {"nodes": 0, "edges": 0}},
       "空库给空图，不抛异常")


# --------------------------------------------------------------------------
# 2. 边：去重、无向归一、悬空链接
# --------------------------------------------------------------------------


@test
def test_edges_are_normalised_source_lt_target():
    for e in graph.compute(corpus())["edges"]:
        ok(e["source"] < e["target"], "每条边 source < target：%r" % (e,))


@test
def test_reciprocal_link_is_one_edge_not_two():
    result = graph.compute(corpus())
    pairs = [(e["source"], e["target"]) for e in result["edges"]]
    eq(len(pairs), len(set(pairs)), "边不许重复")
    eq(pairs.count(("bb", "ee")), 1, "bb 与 ee 互链，仍然只出一条边")


@test
def test_dangling_link_neither_crashes_nor_invents_a_node():
    result = graph.compute(corpus())
    ok(all(n["slug"] != "ghost" for n in result["nodes"]), "悬空链接不能造出节点")
    ok(all("ghost" not in (e["source"], e["target"]) for e in result["edges"]),
       "悬空链接不能造出边")
    eq(node(result, "lone")["degree"], 0, "只挂悬空链接的笔记度数仍是 0")


@test
def test_self_link_is_dropped():
    result = graph.compute([mk("solo", "自指", "我引用我自己 [[solo]]。")])
    eq(result["edges"], [], "自环不是边")
    eq(result["nodes"][0]["degree"], 0, "自环不算度数")


@test
def test_edges_sorted_by_source_then_target():
    edges = graph.compute(corpus())["edges"]
    eq(edges, sorted(edges, key=lambda e: (e["source"], e["target"])), "边按端点排序")


@test
def test_nodes_sorted_by_slug():
    slugs = [n["slug"] for n in graph.compute(corpus())["nodes"]]
    eq(slugs, sorted(slugs), "节点按 slug 排序")


# --------------------------------------------------------------------------
# 3. 理由句
# --------------------------------------------------------------------------


@test
def test_every_edge_carries_a_non_empty_reason():
    for e in graph.compute(corpus())["edges"]:
        ok(isinstance(e["reason"], str) and e["reason"].strip(),
           "边 %s↔%s 要有非空理由句" % (e["source"], e["target"]))
        ok(e["reason_from"] in (e["source"], e["target"]),
           "reason_from 必须是这条边的某一端：%r" % (e,))


@test
def test_reason_is_the_sentence_holding_the_link():
    e = edge(graph.compute(corpus()), "bb", "cc")
    eq(e["reason"], "前提是 [[cc]]。", "理由句是正文里挂着那个链接的那一句")
    eq(e["reason_from"], "bb", "这句话是 bb 写的")


@test
def test_reciprocal_edge_takes_the_source_side_sentence():
    """互链有两句，本视图每条边只给一句：固定取 source 侧，不掷骰子。"""
    e = edge(graph.compute(corpus()), "bb", "ee")
    eq(e["reason"], "它和 [[ee]] 互为反例。", "取 source（bb）侧那句")
    eq(e["reason_from"], "bb", "reason_from 指向真正写这句话的那条笔记")


@test
def test_reason_falls_back_to_the_target_side():
    """source/target 是排过序的一对，不是链接方向。只有 target 写了链接时，
    理由句得从 target 侧取，而 reason_from 必须如实说是它写的。"""
    notes = [mk("pp", "前", "正文里不提对方。"), mk("qq", "后", "它推翻了 [[pp]]。")]
    e = edge(graph.compute(notes), "pp", "qq")
    eq(e["reason"], "它推翻了 [[pp]]。", "理由句来自真正写了链接的那一侧")
    eq(e["reason_from"], "qq", "reason_from 说清这句话是 qq 写的，不能挂在 pp 名下")


@test
def test_reason_matches_exact_slug_not_prefix():
    """挂着 [[aa-longer]] 的句子不能被错认成 aa 的理由。"""
    notes = [
        mk("xx", "x", "先提 [[aa-longer]] 一下。真正的理由在这句 [[aa]]。"),
        mk("aa", "短", "空。"),
        mk("aa-longer", "长", "空。"),
    ]
    eq(edge(graph.compute(notes), "aa", "xx")["reason"], "真正的理由在这句 [[aa]]。",
       "理由句必须精确匹配链接目标，不做前缀子串搜")


@test
def test_unextractable_reason_is_null_not_fabricated():
    """抽不出就给 null。真语料里 sentences() 能兜住任何一行，所以这条防御分支
    只能靠手动制造「有链接、正文里却找不到」的错位来触发。"""
    a, b = mk("pp", "前", "正文里根本不提它。"), mk("qq", "后", "也不提。")
    a.links = ["qq"]
    e = graph.compute([a, b])["edges"][0]
    eq((e["source"], e["target"]), ("pp", "qq"), "边还在")
    eq(e["reason"], None, "抽不出理由句时给 null，不硬造")
    eq(e["reason_from"], None, "没有作者可指认时 reason_from 也是 null")


# --------------------------------------------------------------------------
# 4. 与 related / hubs 是同一张图
# --------------------------------------------------------------------------


@test
def test_edge_set_agrees_with_hubs_build_graph():
    result = graph.compute(corpus())
    _, _, hub_edges = hubs.build_graph(corpus())
    eq({(e["source"], e["target"]) for e in result["edges"]}, hub_edges,
       "graph 与 hubs 必须认同一套边（同一份去重与无向归一规则）")


@test
def test_edges_agree_with_related_on_a_connected_component():
    """从 aa 走够多跳能覆盖整个连通块，那块里的边必须与整图完全一致。"""
    whole = {(e["source"], e["target"]) for e in graph.compute(corpus())["edges"]}
    near = related.compute(corpus(), "aa", hops=10)
    eq({(e["a"], e["b"]) for e in near["edges"]}, whole,
       "related 的诱导子图与 graph 的边集必须一致")


@test
def test_reason_sentences_agree_with_related():
    """同一条链接，两个命令给出的「为什么」必须是同一句话。"""
    near = related.compute(corpus(), "bb", hops=1)
    compared = 0
    for e in graph.compute(corpus())["edges"]:
        for r in next((x["reasons"] for x in near["edges"]
                       if (x["a"], x["b"]) == (e["source"], e["target"])), []):
            if r["from"] == e["reason_from"]:
                compared += 1
                eq(e["reason"], r["sentence"], "同一条边的理由句必须与 related 一致")
    # 没这一条的话，两边哪天错开了边的键名，循环会一次都不进而用例照样绿。
    eq(compared, 4, "bb 的 1 跳邻域里有 4 条边可以两边对照")


# --------------------------------------------------------------------------
# 5. 确定性与纯度
# --------------------------------------------------------------------------


@test
def test_compute_is_deterministic_across_runs():
    a = json.dumps(graph.compute(corpus()), ensure_ascii=False, sort_keys=False)
    b = json.dumps(graph.compute(corpus()), ensure_ascii=False, sort_keys=False)
    eq(a, b, "两次 compute 必须逐字节相同")


@test
def test_input_order_does_not_change_output():
    """load_all 给的顺序变了（换个文件系统、换个 glob 实现），输出不许跟着变。"""
    forward = graph.compute(corpus())
    backward = graph.compute(list(reversed(corpus())))
    eq(backward, forward, "输入顺序不许泄漏进输出")


@test
def test_result_is_json_serialisable():
    result = graph.compute(corpus())
    ok(json.loads(json.dumps(result, ensure_ascii=False)) == result, "结果应能无损过一轮 JSON")


@test
def test_compute_does_not_mutate_input():
    notes = corpus()
    before = [(n.slug, n.body, list(n.links)) for n in notes]
    graph.compute(notes)
    eq([(n.slug, n.body, list(n.links)) for n in notes], before, "compute 不许改输入")


@test
def test_render_smoke():
    text = graph.render(graph.compute(corpus()))
    ok("6 个节点，5 条边" in text, "抬头要报节点数与边数")
    ok("孤儿" in text or "谁也没连上" in text, "孤儿要有一句人话")
    ok(not text.endswith("\n"), "渲染结果不该以空行结尾")
    eq(graph.render(graph.compute([])), "库是空的", "空库要有一句人话")


# --------------------------------------------------------------------------
# 6. CLI
# --------------------------------------------------------------------------

NOTE_TEMPLATE = """---
id: {slug}
title: {title}
evidence: {evidence}
status: {status}
event_date: 2026-08-01
source: tests/test_graph.py 生成的临时语料
---

{body}
"""


def write_note_files(root, notes):
    notes_dir = Path(root) / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    for n in notes:
        (notes_dir / (n.slug + ".md")).write_text(
            NOTE_TEMPLATE.format(
                slug=n.slug,
                title=n.fm.get("title", n.slug),
                evidence=n.evidence,
                status=n.status,
                body=n.body,
            ),
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
def test_cli_fixture_workspace_gives_the_expected_graph():
    tmp = tempfile.mkdtemp(prefix="kb-graph-cli-")
    try:
        write_note_files(tmp, corpus())
        proc = run_kb(["--root", tmp, "graph", "--json"])
        eq(proc.returncode, 0, "应退出 0（stderr: %s）" % proc.stderr[:200])
        eq(json.loads(proc.stdout), EXPECTED, "CLI 给出的整份 payload 必须与期望一致")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@test
def test_cli_json_works_in_both_flag_positions():
    tmp = tempfile.mkdtemp(prefix="kb-graph-flags-")
    try:
        write_note_files(tmp, corpus())
        payloads = []
        for argv in (["--root", tmp, "--json", "graph"],
                     ["--root", tmp, "graph", "--json"],
                     ["graph", "--root", tmp, "--json"]):
            proc = run_kb(argv)
            eq(proc.returncode, 0, "kb %s 应退出 0（stderr: %s）" % (" ".join(argv), proc.stderr[:200]))
            parsed, why = None, ""
            try:
                parsed = json.loads(proc.stdout)
            except ValueError as e:
                why = "，json.loads 报 %s" % e
            ok(parsed is not None, "kb %s 应输出可解析的 JSON%s" % (" ".join(argv), why))
            if parsed is not None:
                payloads.append(json.dumps(parsed, sort_keys=True, ensure_ascii=False))
        eq(len(set(payloads)), 1, "三种 flag 位置应给出完全一样的 JSON")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@test
def test_cli_output_is_byte_identical_across_two_runs():
    tmp = tempfile.mkdtemp(prefix="kb-graph-determinism-")
    try:
        write_note_files(tmp, corpus())
        first = run_kb(["--root", tmp, "graph", "--json"])
        second = run_kb(["--root", tmp, "graph", "--json"])
        eq(first.returncode, 0, "第一次应退出 0")
        eq(second.stdout, first.stdout, "两次跑必须逐字节相同")
        human = [run_kb(["--root", tmp, "graph"]).stdout for _ in range(2)]
        eq(human[1], human[0], "人读输出同样必须逐字节相同")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@test
def test_cli_human_output_is_not_json():
    tmp = tempfile.mkdtemp(prefix="kb-graph-human-")
    try:
        write_note_files(tmp, corpus())
        proc = run_kb(["--root", tmp, "graph"])
        eq(proc.returncode, 0, "人类可读模式应退出 0")
        ok("6 个节点，5 条边" in proc.stdout, "要报节点数与边数")
        ok(not proc.stdout.lstrip().startswith("{"), "无 --json 时不该吐 JSON")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@test
def test_cli_does_not_write_anything():
    """graph 是只读派生视图。跑一趟不许在工作区留下任何东西。"""
    tmp = tempfile.mkdtemp(prefix="kb-graph-readonly-")
    try:
        write_note_files(tmp, corpus())
        before = sorted(p.relative_to(tmp).as_posix() for p in Path(tmp).rglob("*"))
        run_kb(["--root", tmp, "graph", "--json"])
        after = sorted(p.relative_to(tmp).as_posix() for p in Path(tmp).rglob("*"))
        eq(after, before, "跑完之后工作区的文件清单不许变")
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

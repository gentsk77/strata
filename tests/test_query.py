"""kb query 的回归测试。零依赖，直接跑：python tests/test_query.py

退出码 0 = 全过，1 = 有失败。框架照抄 tests/test_hubs.py：图结构类用例在
代码里用 Note 直接构造（一屏看完整张图），CLI 用例落真文件进临时目录跑子进程。

query 没有 MIN_NOTES 门槛（检索 3 条笔记的库完全合法），所以 CLI 语料不用凑
40 条。
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

from kb import query
from kb.notes import Note
from kb.text import sentence_containing, sentences

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
    """手搓一个 Note。links 由正文里的 [[..]] 决定，与真解析一致。"""
    return Note("notes/%s.md" % slug, {"id": slug, "title": title}, body)


def corpus():
    """五条小语料，中英混排，链接方向三种都有：
    steam ↔ dry-oven（互链）、steam → proofing（单向出）、hydration → steam（单向入）。
    lone 是孤儿。悬空链接 [[ghost]] 挂在 proofing 上。
    """
    return [
        mk("steam-sets-the-crust", "蒸汽把脆皮从发闷拉回可用区间",
           "DDT 不变时干烤会让外皮发闷这个说法是错的。铸铁锅的蒸汽把脆皮从零拉回可用区间。\n"
           "这反驳了 [[dry-oven-blocks-the-crust]] 的普遍说法。它的工程含义延伸见 "
           "[[proofing-is-the-moat]]。"),
        mk("dry-oven-blocks-the-crust", "家用烤箱只能干烤是烘焙口头禅",
           "几乎每份食谱都会先说家用烤箱做不出好外皮。反例见 [[steam-sets-the-crust]]。"),
        mk("proofing-is-the-moat", "把面团真发到位本身就是差异化",
           "家用酵种养了十二年仍稳定发起的比例约一成。前提是 [[ghost]] 那套论证成立。"),
        mk("hydration-is-the-category", "高水量是唯一被验证的酸面包品类",
           "Ankarsrum 证明了和面机也能揉出高水量面团。工具教训同源，见 [[steam-sets-the-crust]]。"),
        mk("lone-note", "孤儿笔记完全合法", "没有任何链接的一条。"),
    ]


def hit_slugs(result):
    return [h["slug"] for h in result["hits"]]


def hit(result, slug):
    for h in result["hits"]:
        if h["slug"] == slug:
            return h
    return None


# --------------------------------------------------------------------------
# 1. 匹配范围与大小写
# --------------------------------------------------------------------------


@test
def test_chinese_body_substring_match():
    """中文子串命中正文，snippet 是包含它的那个句子。"""
    result = query.compute(corpus(), "脆皮")
    ok("steam-sets-the-crust" in hit_slugs(result), "正文含「脆皮」的笔记应命中")
    h = hit(result, "steam-sets-the-crust")
    eq(h["snippet"], "铸铁锅的蒸汽把脆皮从零拉回可用区间。",
       "snippet 应是正文里第一个包含检索词的完整句子")
    ok("body" in h["matched_in"], "matched_in 应标出正文命中")
    ok("title" in h["matched_in"], "标题也含「脆皮」，应一并标出")


@test
def test_ascii_match_is_case_insensitive():
    """英文大小写不敏感：查 ddt 命中 DDT，查 ANKARSRUM 命中 Ankarsrum。"""
    lower = query.compute(corpus(), "ddt")
    ok("steam-sets-the-crust" in hit_slugs(lower), "小写 ddt 应命中正文里的 DDT")
    upper = query.compute(corpus(), "ANKARSRUM")
    ok("hydration-is-the-category" in hit_slugs(upper), "大写 ANKARSRUM 应命中正文里的 Ankarsrum")


@test
def test_slug_matches_when_title_is_chinese():
    """标题是中文时，英文检索词靠 slug 接住。"""
    result = query.compute(corpus(), "steam")
    h = hit(result, "steam-sets-the-crust")
    ok(h is not None, "英文词应通过 slug 命中")
    ok("slug" in h["matched_in"], "matched_in 应标出 slug 命中")


@test
def test_no_match_gives_empty_hits():
    result = query.compute(corpus(), "不存在的词xyz")
    eq(result["count"], 0, "没命中时 count 为 0")
    eq(result["hits"], [], "没命中时 hits 为空表")
    eq(result["notes"], 5, "notes 仍报搜了多少条")


@test
def test_needle_is_matched_as_one_phrase_not_words():
    """整段作为一个子串：词序不对就不命中，不做分词 AND。"""
    result = query.compute(corpus(), "脆皮 铸铁锅")
    eq(result["count"], 0, "「脆皮 铸铁锅」在正文里不连续出现，不该命中")


# --------------------------------------------------------------------------
# 2. 邻居
# --------------------------------------------------------------------------


@test
def test_neighbors_carry_direction():
    """三种方向都要标对：out（它指向）、in（指向它）、both（互链）。"""
    result = query.compute(corpus(), "脆皮")
    nbs = {nb["slug"]: nb["direction"] for nb in hit(result, "steam-sets-the-crust")["neighbors"]}
    eq(nbs.get("dry-oven-blocks-the-crust"), "both", "互链应标 both")
    eq(nbs.get("proofing-is-the-moat"), "out", "单向出链应标 out")
    eq(nbs.get("hydration-is-the-category"), "in", "单向入链应标 in")
    eq(len(nbs), 3, "邻居就这三条，不多不少")


@test
def test_dangling_link_is_not_a_neighbor():
    """悬空链接 [[ghost]] 不在库里，不算邻居。"""
    result = query.compute(corpus(), "十二年")
    nbs = [nb["slug"] for nb in hit(result, "proofing-is-the-moat")["neighbors"]]
    ok("ghost" not in nbs, "悬空链接不能凭空造出一个邻居")
    eq(nbs, ["steam-sets-the-crust"], "proofing 的真邻居只有 steam（它的入链）")


@test
def test_orphan_hit_has_empty_neighbors():
    result = query.compute(corpus(), "孤儿")
    h = hit(result, "lone-note")
    ok(h is not None, "孤儿笔记也要能被检索到")
    eq(h["neighbors"], [], "孤儿的邻居是空表，不是缺字段")


# --------------------------------------------------------------------------
# 3. 排序与形状
# --------------------------------------------------------------------------


@test
def test_title_hits_sort_before_body_only_hits():
    """「干烤」同时命中 dry-oven 的标题和 steam 的正文：标题命中排前面。"""
    result = query.compute(corpus(), "干烤")
    slugs = hit_slugs(result)
    eq(slugs[0], "dry-oven-blocks-the-crust", "标题命中的排最前")
    ok("steam-sets-the-crust" in slugs[1:], "只有正文命中的排后面")


@test
def test_snippet_none_for_title_only_hit():
    """只命中标题时 snippet 是 None，不硬造一个正文片段。"""
    result = query.compute(corpus(), "口头禅")
    h = hit(result, "dry-oven-blocks-the-crust")
    ok("body" not in h["matched_in"], "「口头禅」只在标题里")
    eq(h["snippet"], None, "正文没命中就没有 snippet")


@test
def test_snippet_falls_back_to_window_when_needle_crosses_sentences():
    """检索词横跨句号时没有单句包含它，退回原文窗口而不是丢掉。"""
    notes = [mk("cross", "跨句测试", "第一句结束。第二句开始了。")]
    result = query.compute(notes, "结束。第二句")
    h = hit(result, "cross")
    ok(h is not None, "跨句子串应命中")
    ok(h["snippet"] and "结束。第二句" in h["snippet"], "退回窗口也要包含检索词")


@test
def test_empty_query_raises():
    for bad in ("", "   ", "\n"):
        raised = False
        try:
            query.compute(corpus(), bad)
        except query.EmptyQuery:
            raised = True
        ok(raised, "空检索词 %r 应抛 EmptyQuery" % bad)


@test
def test_result_is_json_serialisable():
    result = query.compute(corpus(), "铸铁锅")
    text = json.dumps(result, ensure_ascii=False)
    ok(json.loads(text) == result, "结果应能无损过一轮 JSON")


@test
def test_compute_does_not_mutate_input():
    notes = corpus()
    before = [(n.slug, n.body, list(n.links)) for n in notes]
    query.compute(notes, "脆皮")
    after = [(n.slug, n.body, list(n.links)) for n in notes]
    eq(after, before, "compute 不许改输入")


@test
def test_render_smoke():
    text = query.render(query.compute(corpus(), "脆皮"))
    ok("命中" in text and "脆皮" in text, "渲染要报命中数和检索词")
    ok("steam-sets-the-crust" in text, "渲染要带 slug")
    ok("↔" in text, "互链邻居要用 ↔ 标出")
    ok(not text.endswith("\n"), "渲染结果不该以空行结尾")
    zero = query.render(query.compute(corpus(), "查不到的词"))
    ok(zero.startswith("命中 0 条"), "零命中也要有一行人话")


# --------------------------------------------------------------------------
# 4. 句子工具（text.py 的行为是 query 和 related 共同的地基）
# --------------------------------------------------------------------------


@test
def test_sentences_split_and_strip_markdown():
    body = "- 第一条要点。\n\n## 小标题\n正句一。正句二！"
    eq(sentences(body), ["第一条要点。", "小标题", "正句一。", "正句二！"],
       "列表与标题前缀要剥掉，句末标点要断句")


@test
def test_sentence_containing_finds_wikilink_sentence():
    body = "先说结论。这反驳了 [[dry-oven-blocks-the-crust]] 的普遍说法。收尾。"
    eq(sentence_containing(body, "[[dry-oven-blocks-the-crust]]", fold=False),
       "这反驳了 [[dry-oven-blocks-the-crust]] 的普遍说法。",
       "按 [[链接]] 找理由句是 related 的地基，必须准确")


# --------------------------------------------------------------------------
# 5. CLI
# --------------------------------------------------------------------------

NOTE_TEMPLATE = """---
id: {slug}
title: {title}
evidence: stated
status: settled
event_date: 2026-08-01
source: tests/test_query.py 生成的临时语料
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
    tmp = tempfile.mkdtemp(prefix="kb-query-cli-")
    try:
        write_note_files(tmp, corpus())
        payloads = []
        for argv in (["--root", tmp, "--json", "query", "脆皮"],
                     ["--root", tmp, "query", "脆皮", "--json"],
                     ["query", "脆皮", "--root", tmp, "--json"]):
            proc = run_kb(argv)
            eq(proc.returncode, 0, "kb %s 应退出 0（stderr: %s）" % (" ".join(argv), proc.stderr[:200]))
            parsed, why = None, ""
            try:
                parsed = json.loads(proc.stdout)
            except ValueError as e:
                why = "，json.loads 报 %s" % e
            ok(parsed is not None, "kb %s 应输出可解析的 JSON%s" % (" ".join(argv), why))
            if parsed is not None:
                eq(parsed["notes"], 5, "应读到临时语料的 5 条")
                payloads.append(json.dumps(parsed, sort_keys=True, ensure_ascii=False))
        eq(len(set(payloads)), 1, "三种 flag 位置应给出完全一样的 JSON")

        proc = run_kb(["--root", tmp, "query", "脆皮"])
        eq(proc.returncode, 0, "有命中时人类可读模式退出 0")
        ok("命中" in proc.stdout, "人类可读输出应有抬头")
        ok(not proc.stdout.lstrip().startswith("{"), "无 --json 时不该吐 JSON")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@test
def test_cli_zero_hits_exits_one():
    """grep 惯例：跑成功但零命中，退出 1。JSON 照常输出。"""
    tmp = tempfile.mkdtemp(prefix="kb-query-zero-")
    try:
        write_note_files(tmp, corpus())
        proc = run_kb(["--root", tmp, "--json", "query", "查不到的词xyz"])
        eq(proc.returncode, 1, "零命中应退出 1")
        payload = json.loads(proc.stdout)
        eq(payload["count"], 0, "JSON 里 count 为 0")
        ok("error" not in payload, "零命中不是错误，不该有 error 字段")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@test
def test_cli_empty_query_exits_two_with_json_error():
    tmp = tempfile.mkdtemp(prefix="kb-query-empty-")
    try:
        write_note_files(tmp, corpus())
        proc = run_kb(["--root", tmp, "--json", "query", "   "])
        eq(proc.returncode, 2, "空检索词应退出 2")
        payload = json.loads(proc.stdout)
        eq(payload["error"], "empty_query", "错误有机器可读的类型")
        proc = run_kb(["--root", tmp, "query", "   "])
        eq(proc.returncode, 2, "人类可读模式同样退出 2")
        eq(proc.stdout.strip(), "", "报错时不往 stdout 写内容")
        ok("空" in proc.stderr, "stderr 有一句人话")
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

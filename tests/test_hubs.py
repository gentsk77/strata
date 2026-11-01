"""kb hubs 的回归测试。零依赖，直接跑：python tests/test_hubs.py

退出码 0 = 全过，1 = 有失败。失败会打印文件名:行号和期望/实际。

## 测试数据为什么在代码里造，不放 tests/fixtures/

`hubs.compute` 是纯函数，输入是 `Note` 对象列表，不碰文件系统。它要测的东西
是图结构（谁指向谁、社区怎么切、入口怎么选），而 `MIN_NOTES = 40` 意味着走真
文件路线每个用例都得铺 40 个 markdown 文件。一个「入口平局怎么破」的用例，真正
承载信息的只有四行链接，其余 36 个文件是噪声，而且改一次图结构要动几十个文件。
所以图结构类的用例全部在代码里用 `Note(path, fm, body)` 直接构造，一屏之内能看
完整张图。

两个例外走真文件，因为它们测的恰恰是解析层和 CLI 层：

  - `test_hub_flag_comes_from_parser_as_string`：`hub: true` 经过
    `parse_frontmatter` 之后是**字符串** `"true"` 而不是布尔 `True`。
    `_is_hub_flag` 依赖这个事实，所以这条必须过真解析器，不能用手搓的 fm 字典。
  - CLI 系列：临时目录里现生成 40 个合规笔记文件，跑完删。**不写进
    tests/fixtures/**，那底下的 `notes/` 是给 doctor 用的、故意写坏的夹具
    （应报 8 条违规），往里加 40 个正常笔记会把那个断言毁掉。

## 已知缺陷

文件末尾 `KNOWN_DEFECT_*` 那几条锁定的是**当前行为，不是契约**。修掉对应缺陷后
这些用例会红，那时候改断言，不要改实现去迁就它们。
"""

import datetime
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from kb import graph, hubs
from kb.notes import Note, parse_frontmatter

# 固定「今天」。不注入的话窗口用例会随真实日期漂，某天突然变红而代码没动过。
TODAY = datetime.date(2026, 8, 11)


# --------------------------------------------------------------------------
# 极简断言框架
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
# 造图的积木
# --------------------------------------------------------------------------


def mk(slug, links=(), title=None, event_date=None, hub=None):
    """手搓一个 Note。slug 来自路径 stem，links 来自正文的 [[..]]，与真解析一致。"""
    fm = {"id": slug, "title": title or ("标题 " + slug)}
    if event_date is not None:
        fm["event_date"] = event_date
    if hub is not None:
        fm["hub"] = hub
    body = " ".join("[[%s]]" % t for t in links)
    return Note("notes/%s.md" % slug, fm, body)


def cluster(prefix, n):
    """星形 + 环的稠密团：每个成员指向 core（core 因此入链最多），再指向下一个成员。

    core 拿到 n-1 条团内入链，其他成员各 1 条，入口是谁没有歧义。
    """
    core = "%s-core" % prefix
    members = [core] + ["%s-m%02d" % (prefix, i) for i in range(1, n)]
    out = []
    for i, slug in enumerate(members):
        links = [] if slug == core else [core, members[(i % (n - 1)) + 1]]
        out.append(mk(slug, [x for x in links if x != slug]))
    return out


def base_corpus():
    """40 条：三个稠密团（12 / 12 / 10）+ 6 条孤儿。切分结果人眼可判。"""
    return (
        cluster("aa", 12)
        + cluster("bb", 12)
        + cluster("cc", 10)
        + [mk("zz-lone%d" % i) for i in range(1, 7)]
    )


def replace(notes, slug, new_note):
    """把语料里某条换掉，返回新列表。用来在标准语料上打一个洞。"""
    return [new_note if n.slug == slug else n for n in notes]


def topic_of(result, slug):
    for t in result["topics"]:
        if slug in t["members"]:
            return t
    return None


# --------------------------------------------------------------------------
# 1. MIN_NOTES 门槛
# --------------------------------------------------------------------------


@test
def test_min_notes_gate_raises_not_returns_empty():
    """< 40 条抛 TooSparse，而不是返回一个空结果假装算过了。"""
    notes = base_corpus()
    eq(len(notes), hubs.MIN_NOTES, "标准语料应正好卡在门槛上")

    raised = None
    try:
        hubs.compute(notes[:39], today=TODAY)
    except hubs.TooSparse as e:
        raised = e
    ok(raised is not None, "39 条应该抛 TooSparse，没抛")
    if raised is not None:
        eq(raised.notes, 39, "TooSparse 要带上实际条数")
        ok("39" in str(raised) and "40" in str(raised), "报错文案要同时说清现有条数和门槛")

    empty = None
    try:
        hubs.compute([], today=TODAY)
    except hubs.TooSparse as e:
        empty = e
    ok(empty is not None, "空库也该抛 TooSparse")

    # 正好 40 条不抛。边界在 <，不在 <=。
    result = hubs.compute(notes, today=TODAY)
    eq(result["notes"], 40, "正好 40 条应该正常算出来")


# --------------------------------------------------------------------------
# 2. 建图：悬空链接、自环、重复边
# --------------------------------------------------------------------------


@test
def test_dangling_link_creates_no_node_and_no_edge():
    """指向库里不存在的 slug = 「将来值得写」的标记，不是结构，不能凭空造节点。"""
    notes = base_corpus()
    holed = replace(
        notes, "aa-m01", mk("aa-m01", ["aa-core", "aa-m02", "ghost-note", "another-ghost"])
    )

    nodes, adj, edges = hubs.build_graph(holed)
    eq(len(nodes), 40, "悬空目标不该进节点集")
    ok("ghost-note" not in nodes, "ghost-note 不该成为节点")
    ok("another-ghost" not in adj, "悬空目标不该出现在邻接表里")
    ok(
        all("ghost-note" not in e and "another-ghost" not in e for e in edges),
        "悬空链接不该成边",
    )

    # 边数与没打洞时完全一致：两条悬空链接一条边都没加。
    eq(len(edges), len(hubs.build_graph(notes)[2]), "悬空链接不该改变边数")

    counts = hubs.in_degree(holed)
    ok("ghost-note" not in counts, "悬空目标不该出现在入链统计里")

    result = hubs.compute(holed, today=TODAY)
    all_members = [s for t in result["topics"] for s in t["members"]]
    ok("ghost-note" not in all_members, "悬空目标不该成为任何主题的成员")
    # 孤儿走碎片通道，所以「每条笔记都被分到某处」要连碎片一起数。
    all_members += [s for f in result["fragments"] for s in f["members"]]
    ok("ghost-note" not in all_members, "悬空目标也不该出现在碎片里")
    eq(len(all_members), 40, "主题加碎片应覆盖全部笔记")


@test
def test_self_loop_is_dropped():
    """自己指自己不构成结构。"""
    graph = [mk("a", ["a"]), mk("b", ["a"]), mk("c", ["b"])]
    nodes, adj, edges = hubs.build_graph(graph)
    eq(sorted(edges), [("a", "b"), ("b", "c")], "自环不该成边")
    ok("a" not in adj["a"], "自环不该进邻接表")
    eq(hubs.in_degree(graph)["a"], 1, "自环不该记进入链，a 只有来自 b 的一条")

    # 走完整 compute 也不能被自环污染。
    notes = base_corpus()
    selfy = replace(notes, "aa-core", mk("aa-core", ["aa-core"]))
    eq(
        len(hubs.build_graph(selfy)[2]),
        len(hubs.build_graph(notes)[2]),
        "给 core 加个自环不该改变边数",
    )


@test
def test_duplicate_and_reciprocal_links_count_once():
    """A→B 与 B→A 是同一条无向边；同一条笔记里写两遍也只算一条。"""
    reciprocal = [mk("a", ["b"]), mk("b", ["a"]), mk("c", ["a"])]
    eq(sorted(hubs.build_graph(reciprocal)[2]), [("a", "b"), ("a", "c")], "互指只算一条边")

    # 正文里重复写同一个链接。
    dup = Note("notes/d.md", {"id": "d", "title": "t"}, "[[a]] 中间隔点字 [[a]] [[a]]")
    graph = [mk("a"), dup]
    eq(list(dup.links), ["a"], "同一条笔记里的重复链接应被去重")
    eq(sorted(hubs.build_graph(graph)[2]), [("a", "d")], "重复链接只算一条边")
    eq(hubs.in_degree(graph)["a"], 1, "入链数的是「多少条不同的笔记指向它」，不是链接次数")


# --------------------------------------------------------------------------
# 3. 确定性：选贪心模块度而不是标签传播的唯一理由
# --------------------------------------------------------------------------


def _stable_dump(result):
    return json.dumps(result, sort_keys=True, ensure_ascii=False)


@test
def test_same_input_twice_gives_identical_result():
    """同一份输入跑两次，主题划分、入口、排序、成员顺序完全一致。"""
    notes = base_corpus()
    a = hubs.compute(notes, days=14, today=TODAY)
    b = hubs.compute(notes, days=14, today=TODAY)
    eq(_stable_dump(a), _stable_dump(b), "两次 compute 的结果必须逐字节一致")


@test
def test_result_is_independent_of_input_order():
    """笔记的读入顺序不该影响任何输出。load_all 按文件名排序，但契约不该依赖它。"""
    notes = base_corpus()
    reference = _stable_dump(hubs.compute(notes, days=14, today=TODAY))
    for seed in (1, 7, 99):
        shuffled = list(notes)
        random.Random(seed).shuffle(shuffled)
        eq(
            _stable_dump(hubs.compute(shuffled, days=14, today=TODAY)),
            reference,
            "打乱输入顺序（seed=%d）后结果变了" % seed,
        )


@test
def test_deterministic_across_processes_with_different_hash_seeds():
    """跨进程确定性。

    实现里用了 set（edges）和 dict（members），CPython 默认对字符串哈希加随机盐
    （PYTHONHASHSEED），所以「同一个进程里跑两次一样」证明不了「明天跑一样」。
    这条起三个 hash seed 不同的子进程比对完整 JSON。
    """
    tmp = tempfile.mkdtemp(prefix="kb-hubs-det-")
    try:
        write_note_files(tmp, base_corpus())
        outs = []
        for seed in ("0", "1", "424242"):
            proc = run_kb(["--root", tmp, "--json", "hubs"], env={"PYTHONHASHSEED": seed})
            eq(proc.returncode, 0, "PYTHONHASHSEED=%s 时 kb hubs 应退出 0" % seed)
            payload = json.loads(proc.stdout)
            payload.pop("asof", None)  # 子进程读真实时钟，跨午夜会漂，与确定性无关
            outs.append(json.dumps(payload, sort_keys=True, ensure_ascii=False))
        eq(len(set(outs)), 1, "不同 PYTHONHASHSEED 下结果必须完全一致")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 4. 社区切分：已知答案
# --------------------------------------------------------------------------


@test
def test_two_triangles_joined_by_one_bridge_stay_two_communities():
    """教科书用例：两个三角形加一条桥，模块度最优解是切开，不是合并。"""
    graph = [
        mk("t1", ["t2", "t3"]),
        mk("t2", ["t3"]),
        mk("t3", []),
        mk("u1", ["u2", "u3"]),
        mk("u2", ["u3"]),
        mk("u3", ["t3"]),  # 桥
    ]
    nodes, _, edges = hubs.build_graph(graph)
    eq(len(edges), 7, "三角形各 3 条边加一条桥")
    parts = sorted(hubs.greedy_modularity(nodes, edges).values())
    eq(parts, [["t1", "t2", "t3"], ["u1", "u2", "u3"]], "一条桥不该把两个三角形并成一团")


@test
def test_designed_clusters_are_recovered():
    """三个设计好的稠密团应被原样切出来；孤儿是碎片，不是主题。"""
    result = hubs.compute(base_corpus(), days=14, today=TODAY)
    sizes = [t["size"] for t in result["topics"]]
    eq(sizes, [12, 12, 10], "应切出 12/12/10 三个团")
    eq([f["size"] for f in result["fragments"]], [1] * 6, "6 条孤儿各自是碎片")

    aa = topic_of(result, "aa-core")
    eq(
        aa["members"],
        ["aa-core"] + ["aa-m%02d" % i for i in range(1, 12)],
        "aa 团的成员应完全等于设计的那 12 条",
    )
    ok(
        all(s.startswith("bb-") for s in topic_of(result, "bb-core")["members"]),
        "bb 团不该混进别的团的成员",
    )


@test
def test_topics_sorted_by_size_then_entry_slug():
    """大主题在前；同样大小按入口 slug 定序，保证输出稳定。"""
    result = hubs.compute(base_corpus(), days=14, today=TODAY)
    keys = [(-t["size"], t["entry"]) for t in result["topics"]]
    eq(keys, sorted(keys), "topics 没有按 (-size, entry) 排好")
    # aa 与 bb 都是 12 条，平局，字典序小的 aa-core 应在前。
    eq(
        [t["entry"] for t in result["topics"][:3]],
        ["aa-core", "bb-core", "cc-core"],
        "同样大小时应按入口 slug 定序",
    )


# --------------------------------------------------------------------------
# 5. 入口选择
# --------------------------------------------------------------------------


@test
def test_entry_is_the_note_with_most_in_topic_links():
    """入口 = 社区内入链最多的那条。"""
    result = hubs.compute(base_corpus(), days=14, today=TODAY)
    for prefix, size in (("aa", 12), ("bb", 12), ("cc", 10)):
        topic = topic_of(result, "%s-core" % prefix)
        eq(topic["entry"], "%s-core" % prefix, "%s 团的入口应是 core" % prefix)
        eq(topic["name"], "标题 %s-core" % prefix, "主题名应直接用入口笔记的 title")
        eq(topic["entry_pinned"], False, "没标 hub: true 时不该说是指定的")
        eq(topic["top_members"][0]["slug"], "%s-core" % prefix, "top_members 第一个应是入口")
        eq(topic["top_members"][0]["in_topic"], size - 1, "core 应收到其余全部成员的入链")
        ok(len(topic["top_members"]) <= 5, "top_members 最多 5 条")


@test
def test_in_topic_links_beat_global_links():
    """社区内入链优先于全局入链：跨主题的热门笔记不能抢走入口位。

    图：tt 团四条笔记，tt-alpha / tt-omega 各收 2 条团内入链，tt-y 只收 1 条，
    但 aa 团有两条笔记跨过来指 tt-y，让它的全局入链变成 3（团内最高）。
    入口仍必须是 tt-alpha。
    """
    aa = cluster("aa", 12)
    aa = replace(aa, "aa-m03", mk("aa-m03", ["aa-core", "aa-m04", "tt-y"]))
    aa = replace(aa, "aa-m08", mk("aa-m08", ["aa-core", "aa-m09", "tt-y"]))
    tt = [
        mk("tt-x", ["tt-alpha", "tt-omega"]),
        mk("tt-y", ["tt-alpha", "tt-omega"]),
        mk("tt-alpha", ["tt-x"]),
        mk("tt-omega", ["tt-y"]),
    ]
    notes = aa + cluster("bb", 12) + cluster("cc", 10) + tt + [mk("zz-l1"), mk("zz-l2")]
    eq(len(notes), 40, "语料应正好 40 条")

    global_in = hubs.in_degree(notes)
    eq(global_in["tt-y"], 3, "tt-y 的全局入链应是团内最高的 3")
    eq(global_in["tt-alpha"], 2, "tt-alpha 的全局入链应是 2")

    result = hubs.compute(notes, days=14, today=TODAY)
    topic = topic_of(result, "tt-alpha")
    eq(sorted(topic["members"]), ["tt-alpha", "tt-omega", "tt-x", "tt-y"], "tt 团不该被并掉")
    eq(topic["entry"], "tt-alpha", "全局入链最高的 tt-y 不该抢走入口位")
    by_slug = {m["slug"]: m for m in topic["top_members"]}
    eq(by_slug["tt-y"]["in_topic"], 1, "tt-y 的团内入链是 1")
    eq(by_slug["tt-y"]["in_total"], 3, "tt-y 的全局入链是 3")


@test
def test_entry_tie_break_is_stable():
    """团内入链平局时：先比全局入链，再比 slug 字典序。两级都必须稳定。"""
    tt = [
        mk("tt-x", ["tt-alpha", "tt-omega"]),
        mk("tt-y", ["tt-alpha", "tt-omega"]),
        mk("tt-alpha", ["tt-x"]),
        mk("tt-omega", ["tt-y"]),
    ]
    filler = cluster("aa", 12) + cluster("bb", 12) + cluster("cc", 10)
    notes = filler + tt + [mk("zz-l1"), mk("zz-l2")]

    result = hubs.compute(notes, days=14, today=TODAY)
    topic = topic_of(result, "tt-alpha")
    by_slug = {m["slug"]: m for m in topic["top_members"]}
    eq(by_slug["tt-alpha"]["in_topic"], by_slug["tt-omega"]["in_topic"], "这两条应该团内入链打平")
    eq(by_slug["tt-alpha"]["in_total"], by_slug["tt-omega"]["in_total"], "全局入链也应打平")
    eq(topic["entry"], "tt-alpha", "全平时应取 slug 字典序小的")

    # 全局入链打破平局：aa 团一条笔记跨过来指 tt-omega。
    aa = replace(cluster("aa", 12), "aa-m05", mk("aa-m05", ["aa-core", "aa-m06", "tt-omega"]))
    notes2 = aa + cluster("bb", 12) + cluster("cc", 10) + tt + [mk("zz-l1"), mk("zz-l2")]
    result2 = hubs.compute(notes2, days=14, today=TODAY)
    topic2 = topic_of(result2, "tt-alpha")
    eq(hubs.in_degree(notes2)["tt-omega"], 3, "tt-omega 的全局入链应被抬到 3")
    eq(topic2["entry"], "tt-omega", "团内入链打平时应由全局入链决出入口")

    # 平局的解法本身也得确定：打乱输入顺序不改变结果。
    for seed in (3, 11):
        shuffled = list(notes)
        random.Random(seed).shuffle(shuffled)
        eq(
            topic_of(hubs.compute(shuffled, days=14, today=TODAY), "tt-alpha")["entry"],
            "tt-alpha",
            "平局的 tie-break 必须与输入顺序无关（seed=%d）" % seed,
        )


# --------------------------------------------------------------------------
# 6. hub: true 覆盖
# --------------------------------------------------------------------------


@test
def test_hub_flag_overrides_the_algorithm_entry():
    """标了 hub: true 的那条优先当入口，哪怕它的入链不是最多。"""
    notes = base_corpus()
    flagged = replace(notes, "aa-m07", mk("aa-m07", ["aa-core", "aa-m08"], hub="true"))

    plain = topic_of(hubs.compute(notes, today=TODAY), "aa-core")
    eq(plain["entry"], "aa-core", "没标之前入口是 aa-core")

    topic = topic_of(hubs.compute(flagged, today=TODAY), "aa-m07")
    eq(topic["entry"], "aa-m07", "标了 hub: true 的那条应当入口")
    eq(topic["entry_pinned"], True, "被指定时 entry_pinned 应为 True")
    eq(topic["name"], "标题 aa-m07", "主题名应换成被指定那条的 title")
    # 只影响所在的那个团。
    eq(topic_of(hubs.compute(flagged, today=TODAY), "bb-core")["entry"], "bb-core",
       "hub: true 不该影响别的主题")


@test
def test_hub_flag_comes_from_parser_as_string_not_bool():
    """frontmatter 解析器把 hub: true 存成字符串 "true"，不是布尔 True。

    `_is_hub_flag` 依赖这个事实。这条用例过真解析器，防止哪天解析器改成返回布尔
    而覆盖功能静默失效。
    """
    text = "---\nid: x\ntitle: t\nhub: true\n---\n\n正文 [[y]]\n"
    fm, body = parse_frontmatter(text, "x.md")
    eq(fm.get("hub"), "true", "解析器应把 hub: true 存成字符串")
    ok(isinstance(fm.get("hub"), str), "hub 的值应是 str，不是 bool")
    ok(hubs._is_hub_flag(Note("notes/x.md", fm, body)), "字符串 'true' 应被认作 hub 标记")

    # 用真解析出来的 fm 走一遍完整 compute。
    notes = base_corpus()
    real = Note("notes/aa-m07.md", fm, "[[aa-core]] [[aa-m08]]")
    eq(topic_of(hubs.compute(replace(notes, "aa-m07", real), today=TODAY), "aa-m07")["entry"],
       "aa-m07", "真解析出来的 hub 标记也应生效")


@test
def test_hub_flag_falsy_and_missing_values_do_not_override():
    """只有真值才覆盖。写错、留空、不写都必须退回算法结果。"""
    notes = base_corpus()
    for raw in ("false", "no", "0", "", {}, "maybe"):
        flagged = replace(notes, "aa-m07", mk("aa-m07", ["aa-core", "aa-m08"], hub=raw))
        topic = topic_of(hubs.compute(flagged, today=TODAY), "aa-core")
        eq(topic["entry"], "aa-core", "hub=%r 不该覆盖入口" % (raw,))
        eq(topic["entry_pinned"], False, "hub=%r 时 entry_pinned 应为 False" % (raw,))

    for raw in ("true", "True", "TRUE", " true ", "yes", "1"):
        flagged = replace(notes, "aa-m07", mk("aa-m07", ["aa-core", "aa-m08"], hub=raw))
        eq(topic_of(hubs.compute(flagged, today=TODAY), "aa-core")["entry"], "aa-m07",
           "hub=%r 应该覆盖入口" % (raw,))


@test
def test_multiple_hub_flags_in_one_topic_pick_deterministically():
    """同一个团里标了两条时，取排名靠前的那条，且与输入顺序无关。"""
    notes = base_corpus()
    notes = replace(notes, "aa-m07", mk("aa-m07", ["aa-core", "aa-m08"], hub="true"))
    notes = replace(notes, "aa-m03", mk("aa-m03", ["aa-core", "aa-m04"], hub="true"))

    entry = topic_of(hubs.compute(notes, today=TODAY), "aa-core")["entry"]
    eq(entry, "aa-m03", "两条都标时应取 (团内入链, 全局入链, slug) 排名靠前的")
    for seed in (2, 5, 13):
        shuffled = list(notes)
        random.Random(seed).shuffle(shuffled)
        eq(topic_of(hubs.compute(shuffled, today=TODAY), "aa-core")["entry"], entry,
           "多个 hub 标记的选择必须与输入顺序无关（seed=%d）" % seed)


# --------------------------------------------------------------------------
# 7. 时间窗口
# --------------------------------------------------------------------------


def dated_corpus(offsets):
    """把 aa 团前几条的 event_date 设成「today 减 N 天」。offsets 是 slug -> 天数。"""
    notes = base_corpus()
    for slug, days_ago in offsets.items():
        old = next(n for n in notes if n.slug == slug)
        notes = replace(
            notes,
            slug,
            mk(slug, old.links, event_date=(TODAY - datetime.timedelta(days=days_ago)).isoformat()),
        )
    return notes


@test
def test_window_boundary_is_inclusive_at_exactly_n_days():
    """正好 N 天前的笔记算在窗口内，N+1 天前的不算。"""
    notes = dated_corpus({"aa-m01": 0, "aa-m02": 13, "aa-m03": 14, "aa-m04": 15, "aa-m05": 40})
    topic = topic_of(hubs.compute(notes, days=14, today=TODAY), "aa-core")
    eq(topic["recent_members"], ["aa-m01", "aa-m02", "aa-m03"],
       "窗口应含 0/13/14 天前三条，不含 15 与 40 天前的")
    eq(topic["active"], True, "窗口内有新成员的主题应标为活跃")

    # 窗口收到 13 天，正好 13 天前的仍在内，14 天前的掉出去。
    eq(topic_of(hubs.compute(notes, days=13, today=TODAY), "aa-core")["recent_members"],
       ["aa-m01", "aa-m02"], "days=13 时边界应落在 13 天前")


@test
def test_today_is_injectable_and_window_moves_with_it():
    """today 参数必须真的被用上，否则窗口用例会随真实日期漂。"""
    notes = dated_corpus({"aa-m01": 0, "aa-m03": 14, "aa-m04": 15})
    eq(hubs.compute(notes, days=14, today=TODAY)["asof"], TODAY.isoformat(),
       "asof 应回显注入的 today")

    # 把 today 往后推 10 天，原本 0 / 14 天前的两条里只剩最近那条还在窗口内。
    later = TODAY + datetime.timedelta(days=10)
    shifted = hubs.compute(notes, days=14, today=later)
    eq(shifted["asof"], later.isoformat(), "asof 应跟着注入的 today 走")
    eq(topic_of(shifted, "aa-core")["recent_members"], ["aa-m01"],
       "today 推后 10 天后只有最近那条还在窗口内")

    # 再往后推到全部掉出窗口。
    far = TODAY + datetime.timedelta(days=100)
    far_topic = topic_of(hubs.compute(notes, days=14, today=far), "aa-core")
    eq(far_topic["recent_members"], [], "全部掉出窗口后不该还有新成员")
    eq(far_topic["active"], False, "没有窗口内成员的主题不该标为活跃")

    eq(hubs.compute(notes, days=14, today=TODAY)["window_days"], 14, "window_days 应回显 days")
    eq(hubs.compute(notes, days=3, today=TODAY)["window_days"], 3, "window_days 应回显 days")


@test
def test_missing_or_malformed_event_date_is_never_recent():
    """没有 event_date、或者写成「上周」这种，都不算窗口内成员，也不能炸。"""
    notes = base_corpus()  # 全部没有 event_date
    for topic in hubs.compute(notes, days=14, today=TODAY)["topics"]:
        eq(topic["recent_members"], [], "没有 event_date 的笔记不该被算进窗口")
        eq(topic["active"], False, "没有 event_date 时主题不该是活跃的")

    broken = notes
    for slug, raw in (("aa-m01", "上周"), ("aa-m02", ""), ("aa-m03", "2026-13-45"),
                      ("aa-m04", "2026/08/01")):
        old = next(n for n in broken if n.slug == slug)
        broken = replace(broken, slug, mk(slug, old.links, event_date=raw))
    topic = topic_of(hubs.compute(broken, days=14, today=TODAY), "aa-core")
    eq(topic["recent_members"], [], "写坏的 event_date 应被当作没有，而不是炸掉或算成最近")


# --------------------------------------------------------------------------
# 8. 结果结构与渲染
# --------------------------------------------------------------------------


@test
def test_result_shape_is_json_serialisable():
    """compute 的返回值是给 agent 消费的 API，必须能直接进 json.dumps。"""
    result = hubs.compute(dated_corpus({"aa-m01": 1}), days=14, today=TODAY)
    eq(sorted(result.keys()),
       ["asof", "edges", "fragments", "notes", "summary", "topics", "window_days"],
       "顶层字段不该多也不该少")
    ok(json.loads(json.dumps(result, ensure_ascii=False)) == result, "结果应可 JSON 往返")

    topic = result["topics"][0]
    eq(sorted(topic.keys()),
       ["active", "cohesion", "entry", "entry_betweenness", "entry_pinned", "entry_tied",
        "entry_tied_with", "members", "name", "named", "recent_members", "size",
        "top_members"],
       "主题字段不该多也不该少")
    eq(sorted(topic["cohesion"].keys()),
       ["best_bridge", "block_count", "blocks", "bridge_count", "cycle_ratio", "cycles",
        "flags", "grade", "internal_edges", "near_misses", "stitches"],
       "内聚度字段不该多也不该少")
    eq(topic["size"], len(topic["members"]), "size 应等于成员数")
    eq(topic["members"], sorted(topic["members"]), "成员列表应有序")
    eq(topic["recent_members"], sorted(topic["recent_members"]), "窗口内成员列表应有序")
    ok(set(topic["recent_members"]) <= set(topic["members"]), "窗口内成员必须是本主题成员")
    ok(topic["entry"] in topic["members"], "入口必须是本主题成员")
    eq(sorted(topic["top_members"][0].keys()), ["in_topic", "in_total", "slug", "title"],
       "top_members 的字段不该多也不该少")
    eq(result["edges"], len(hubs.build_graph(dated_corpus({"aa-m01": 1}))[2]), "edges 应等于边数")


@test
def test_compute_does_not_mutate_its_input():
    """纯函数：跑完之后输入的 Note 一个字段都不能变。"""
    notes = dated_corpus({"aa-m01": 1})
    before = [(n.slug, dict(n.fm), list(n.links), n.body) for n in notes]
    hubs.compute(notes, days=14, today=TODAY)
    hubs.compute(notes, days=3, today=TODAY)
    after = [(n.slug, dict(n.fm), list(n.links), n.body) for n in notes]
    eq(after, before, "compute 不该改动输入的笔记")


@test
def test_render_smoke():
    """人类可读输出是 JSON 的皮肤：不炸，且关键信息都在。"""
    notes = replace(dated_corpus({"aa-m01": 1}), "cc-m05",
                    mk("cc-m05", ["cc-core", "cc-m06"], hub="true"))
    result = hubs.compute(notes, days=14, today=TODAY)
    text = hubs.render(result)

    ok("40 条笔记" in text, "抬头应报笔记数")
    ok("窗口 14 天" in text and TODAY.isoformat() in text, "抬头应报窗口和截止日期")
    ok("标题 aa-core" in text, "应打印主题名")
    ok("aa-core" in text, "应打印入口 slug")
    # 「活跃」不再印成徽章：真库上 10 个主题全部活跃，一个永远为真的标记
    # 不区分任何东西。改印比例，让读的人自己看得出它有没有信号。
    ok("窗口内新增" in text, "应报窗口内新增的比例")
    ok("hub: true 指定" in text, "被指定的入口应标出来")
    ok("aa-m01" in text, "应列出窗口内新成员")
    ok(not text.endswith("\n"), "渲染结果不该以空行结尾")


# --------------------------------------------------------------------------
# 9. CLI
# --------------------------------------------------------------------------

NOTE_TEMPLATE = """---
id: {slug}
title: {title}
evidence: stated
status: settled
event_date: {event_date}
source: tests/test_hubs.py 生成的临时语料
{extra}---

{body}
"""


def write_note_files(root, notes):
    """把内存里的语料落成真 markdown 文件，用于跑 CLI。字段齐全，doctor 也能过。"""
    notes_dir = Path(root) / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    for n in notes:
        extra = ""
        if n.fm.get("hub"):
            extra = "hub: %s\n" % n.fm["hub"]
        (notes_dir / (n.slug + ".md")).write_text(
            NOTE_TEMPLATE.format(
                slug=n.slug,
                title=n.fm.get("title", n.slug),
                event_date=n.fm.get("event_date") or "2026-08-01",
                extra=extra,
                body=n.body or "（无链接）",
            ),
            encoding="utf-8",
        )
    return str(notes_dir)


def run_kb(args, env=None):
    environ = dict(os.environ)
    environ["PYTHONIOENCODING"] = "utf-8"
    if env:
        environ.update(env)
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
    """`kb --json hubs` 与 `kb hubs --json` 都要出可解析的 JSON。"""
    tmp = tempfile.mkdtemp(prefix="kb-hubs-cli-")
    try:
        write_note_files(tmp, base_corpus())
        payloads = []
        for argv in (["--root", tmp, "--json", "hubs"], ["--root", tmp, "hubs", "--json"],
                     ["hubs", "--root", tmp, "--json"]):
            proc = run_kb(argv)
            eq(proc.returncode, 0, "kb %s 应退出 0（stderr: %s）" % (" ".join(argv), proc.stderr[:200]))
            parsed, why = None, ""
            try:
                parsed = json.loads(proc.stdout)
            except ValueError as e:
                why = "，json.loads 报 %s" % e
            ok(parsed is not None, "kb %s 应输出可解析的 JSON%s" % (" ".join(argv), why))
            if parsed is not None:
                eq(parsed["notes"], 40, "kb %s 应读到临时语料的 40 条" % " ".join(argv))
                payloads.append(json.dumps(parsed, sort_keys=True, ensure_ascii=False))
        eq(len(set(payloads)), 1, "两种 flag 位置应给出完全一样的 JSON")

        # --root 必须真的被读进去，而不是静默滑回真 repo：换一个条数不同的语料，
        # 输出的条数必须跟着变。
        other = tempfile.mkdtemp(prefix="kb-hubs-cli2-")
        try:
            write_note_files(other, base_corpus() + [mk("zz-extra%d" % i) for i in range(1, 4)])
            proc = run_kb(["--root", other, "--json", "hubs"])
            eq(proc.returncode, 0, "换一个 --root 也应退出 0")
            eq(json.loads(proc.stdout)["notes"], 43, "--root 应决定读哪个 notes/ 目录")
        finally:
            shutil.rmtree(other, ignore_errors=True)

        # 人类可读输出
        proc = run_kb(["--root", tmp, "hubs"])
        eq(proc.returncode, 0, "kb hubs 无 --json 时应退出 0")
        ok("条笔记" in proc.stdout, "人类可读输出应有抬头")
        ok(not proc.stdout.lstrip().startswith("{"), "无 --json 时不该吐 JSON")

        # --days 透传
        proc = run_kb(["--root", tmp, "hubs", "--json", "--days", "3"])
        eq(proc.returncode, 0, "kb hubs --days 3 应退出 0")
        eq(json.loads(proc.stdout)["window_days"], 3, "--days 应透传到结果里")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@test
def test_cli_too_sparse_exits_nonzero():
    """笔记不够时 CLI 应退出 1 并在 stderr 说明原因。"""
    tmp = tempfile.mkdtemp(prefix="kb-hubs-sparse-")
    try:
        write_note_files(tmp, base_corpus()[:39])
        proc = run_kb(["--root", tmp, "hubs"])
        eq(proc.returncode, 1, "笔记不够时应退出 1")
        ok("39" in proc.stderr and "40" in proc.stderr, "stderr 应说清现有条数与门槛")
        eq(proc.stdout.strip(), "", "报错时不该往 stdout 写人类可读输出")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 10. 已知缺陷：锁定当前行为，不是背书
# --------------------------------------------------------------------------


@test
def test_too_sparse_path_still_emits_json():
    """错误路径也必须有 JSON 形态。

    derived-views.md §6：JSON 是给 agent 消费的正式 API。空 stdout 加一个非零
    退出码，agent 做 json.loads 会抛异常，问不出「为什么没结果」。
    """
    tmp = tempfile.mkdtemp(prefix="kb-hubs-sparse-json-")
    try:
        write_note_files(tmp, base_corpus()[:39])
        proc = run_kb(["--root", tmp, "--json", "hubs"])
        eq(proc.returncode, 1, "笔记不够仍然退出 1")
        payload = json.loads(proc.stdout)
        eq(payload["error"], "too_sparse", "错误有机器可读的类型")
        eq(payload["notes"], 39, "带上实际笔记数")
        eq(payload["min_notes"], hubs.MIN_NOTES, "带上门槛值，agent 能自己算差多少")
        ok(payload["reason"], "有一句人话解释")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@test
def test_pinned_entry_always_appears_in_top_members():
    """主题名就是入口的标题，入口必须在 top_members 里列得出来。

    否则屏幕上会出现一个主题，名字是入口那条的标题，底下五条里却没有它。
    """
    notes = base_corpus()
    flagged = replace(notes, "aa-m07", mk("aa-m07", ["aa-core", "aa-m08"], hub="true"))
    topic = topic_of(hubs.compute(flagged, today=TODAY), "aa-m07")
    eq(topic["entry"], "aa-m07", "入口确实被覆盖了")
    eq([m["slug"] for m in topic["top_members"]][0], "aa-m07", "入口置顶")
    ok(
        any(m["slug"] == topic["entry"] for m in topic["top_members"]),
        "入口一定出现在 top_members 里",
    )
    eq(len(topic["top_members"]), 5, "置顶不改变列出的条数")


@test
def test_entry_pinned_only_when_the_flag_changed_the_outcome():
    """标记没改变结果时不报告为覆盖，否则读的人以为不标就会变成别的。"""
    notes = base_corpus()
    noop = replace(notes, "aa-core", mk("aa-core", [], hub="true"))
    topic = topic_of(hubs.compute(noop, today=TODAY), "aa-core")
    eq(topic["entry"], "aa-core", "算法本来就选 aa-core")
    eq(topic["entry_pinned"], False, "标记没起作用，不报告为指定")
    ok("hub: true 指定" not in hubs.render(hubs.compute(noop, today=TODAY)),
       "render 也不打那句误导性标注")

    real = replace(notes, "aa-m07", mk("aa-m07", ["aa-core", "aa-m08"], hub="true"))
    changed = topic_of(hubs.compute(real, today=TODAY), "aa-m07")
    eq(changed["entry_pinned"], True, "真的改变了结果时才为 True")


@test
def test_edgeless_corpus_is_rejected_not_split_into_singletons():
    """门槛卡的是「图够不够密」，不只是「笔记够不够多」。

    40 条互不链接的笔记如果放行，产出 40 个 size=1 的「主题」，正是门槛
    想挡的噪声。
    """
    isolated = [mk("solo-%02d" % i) for i in range(40)]
    try:
        hubs.compute(isolated, days=14, today=TODAY)
    except hubs.TooSparse as e:
        eq(e.notes, 40, "笔记数够")
        eq(e.edges, 0, "但一条边都没有")
        ok("边" in e.reason, "理由指向边数而不是笔记数")
    else:
        ok(False, "零边的语料必须抛 TooSparse")


@test
def test_tiny_components_are_fragments_not_topics():
    """小于 MIN_TOPIC_SIZE 的连通分量单独列，不和真主题并排。"""
    notes = base_corpus() + [
        mk("island-a", ["island-b"]),
        mk("island-b", ["island-a"]),
    ]
    result = hubs.compute(notes, today=TODAY)
    topic_members = {s for t in result["topics"] for s in t["members"]}
    ok("island-a" not in topic_members, "碎片不进主题表")
    frag_members = {s for f in result["fragments"] for s in f["members"]}
    # base_corpus() 本身带 6 条孤儿笔记，它们同样是碎片，所以这里只断言
    # 新加的这个 2 点小岛落进了碎片区，不断言碎片区里只有它。
    ok({"island-a", "island-b"} <= frag_members, "小岛整个落进碎片区")
    ok(result["summary"]["fragments"] >= 1, "summary 里数得出来")


@test
def test_summary_grades_sum_to_the_topic_count():
    """汇总各档之和必须等于主题数。

    回归：曾经按 flags 计数而 grade 按严重度取一，同时命中「缝合」和「树」的
    主题被数两次，于是第一行就自相矛盾，4+3+2=9 而主题有 10 个。
    """
    result = hubs.compute(base_corpus(), today=TODAY)
    by_grade = result["summary"]["by_grade"]
    eq(sum(by_grade.values()), len(result["topics"]), "各档之和应等于主题数")
    eq(sum(by_grade.values()), result["summary"]["topics"], "也应等于 summary 里的主题数")
    for t in result["topics"]:
        ok(t["cohesion"]["grade"] in by_grade, "每个主题的等级都要在汇总里有桶")


@test
def test_entry_tiebreak_uses_in_topic_betweenness_not_global():
    """平票用的必须是团内介数。

    回归：跑全图介数会选出「最会跨主题串门的那条」，而那恰好是「团内地基」的
    反面。真库上出现过全图介数最高、团内介数只有 1.0 的笔记被推上主题名。

    构造：aa 团里 aa-bridge 是唯一通往 bb 团的出口（全图介数极高），但它在
    aa 团内部只挂在 aa-core 上（团内介数为 0）。它和 aa-hub 入链打平时，
    必须选 aa-hub。
    """
    notes = [
        # aa 团：core 与 hub 互连，另有三条都指向 hub 和 core
        mk("aa-core", ["aa-hub", "aa-bridge"]),
        mk("aa-hub", ["aa-core"]),
        mk("aa-m1", ["aa-hub", "aa-core"]),
        mk("aa-m2", ["aa-hub", "aa-core"]),
        mk("aa-m3", ["aa-hub", "aa-core"]),
        # 唯一的跨团出口，团内只有一条边
        mk("aa-bridge", ["bb-core"]),
    ]
    notes += [mk("bb-core", ["bb-%d" % i for i in range(1, 5)])]
    notes += [mk("bb-%d" % i, ["bb-core", "bb-%d" % (i % 4 + 1)]) for i in range(1, 5)]
    notes += [mk("cc-%02d" % i, ["cc-%02d" % ((i + 1) % 30)]) for i in range(30)]

    result = hubs.compute(notes, today=TODAY)
    aa = topic_of(result, "aa-hub")
    ok("aa-bridge" in aa["members"], "前提：bridge 与 hub 同团")
    ok(aa["entry"] != "aa-bridge", "跨团出口不该拿到主题名")

    nodes, adj, _ = hubs.build_graph(notes)
    _, sub_adj, _ = graph.subgraph(aa["members"], adj)
    local = graph.betweenness(aa["members"], sub_adj)
    eq(aa["entry_betweenness"], round(local[aa["entry"]], 1),
       "报出来的介数应是团内介数，不是全图介数")
    ok(local["aa-bridge"] <= local[aa["entry"]],
       "入口的团内介数不该低于那条跨团出口")


@test
def test_multi_block_topic_is_graded_and_lists_its_blocks():
    """散成好几块又没有主干的团，要判「疑似多个主题」并列出那几块。

    回归：只查单边割时，库里最大的那个 20 条团（五块拼成、无主干）被判成
    「薄」，而「薄」会被理解成「链接还没补够」，不是「这个名字覆盖不到
    里面 75% 的成员」。
    """
    # 直接测判级函数：顶层社区检测本来就会把一条松散的链拆开，走 compute
    # 反而测不到「一个团内部散成几块」这件事本身。
    # 五块 4 点小团，每块内部成环，相邻块之间一条边，就是真库里那个 20 条团的形状。
    # 每块 5 个点成一个环，回路密度 5/25 = 0.20，和真库那个团的 0.15 同量级。
    blocks = [["b%d-%d" % (b, i) for i in range(5)] for b in range(5)]
    members = sorted(n for blk in blocks for n in blk)
    sub_adj = {n: set() for n in members}

    def link(a, b):
        sub_adj[a].add(b)
        sub_adj[b].add(a)

    edge_count = 0
    for blk in blocks:
        for i in range(5):
            link(blk[i], blk[(i + 1) % 5])
            edge_count += 1
    for b in range(4):
        link(blocks[b][0], blocks[b + 1][0])
        edge_count += 1
    sub_adj = {k: sorted(v) for k, v in sub_adj.items()}

    c = hubs._cohesion(members, sub_adj, edge_count)
    eq(c["grade"], "疑似多个主题", "五块串成的链应判疑似多个主题")
    ok(len(c["blocks"]) >= hubs.MULTI_MIN_BLOCKS, "应列出那几块")
    ok(all(len(b) >= hubs.MULTI_BLOCK_MIN for b in c["blocks"]),
       "只列有分量的块，两条笔记连一起是噪声")

    # 反面：一个有主干的稠密团不该被判成多块。
    hub_members = ["h-%02d" % i for i in range(12)]
    hub_adj = {n: set() for n in hub_members}
    dense = 0
    for n in hub_members[1:]:
        hub_adj[n].add(hub_members[0])
        hub_adj[hub_members[0]].add(n)
        dense += 1
    for i in range(1, 11):
        hub_adj[hub_members[i]].add(hub_members[i + 1])
        hub_adj[hub_members[i + 1]].add(hub_members[i])
        dense += 1
    hub_adj = {k: sorted(v) for k, v in hub_adj.items()}
    eq(hubs._cohesion(hub_members, hub_adj, dense)["grade"], "实",
       "有主干的稠密团应判实")


@test
def test_grade_implies_its_evidence_is_present():
    """等级不能空口说白话：判了就得给出可核对的证据。"""
    for corpus in (base_corpus(), dated_corpus({"aa-m01": 1})):
        for t in hubs.compute(corpus, today=TODAY)["topics"]:
            c = t["cohesion"]
            if c["grade"] == "疑似两个主题":
                ok(c["stitches"], "判疑似两个主题必须给出那条边")
            if c["grade"] == "疑似多个主题":
                ok(c["blocks"], "判疑似多个主题必须列出那几块")
                ok(all(b["name"] and b["entry"] for b in c["blocks"]),
                   "每一块要给出它自己的入口和名字，不然「有五块」这句话没用")
                eq(t["named"], False, "没有主干的团不该借一条笔记的标题当名字")
            if c["grade"] == "实":
                ok(not c["stitches"], "判实就不该同时报缝合点")


@test
def test_negative_grades_still_show_their_margin():
    """阴性也要出示证据。

    回归：「实」曾经只表示「没有任何探测器响」，于是最佳桥平衡度 0.385（离
    0.40 只差 0.015）的团和 0.25 的团印出来一模一样，漏报正好藏在那里。
    """
    for t in hubs.compute(base_corpus(), today=TODAY)["topics"]:
        c = t["cohesion"]
        ok("best_bridge" in c, "每个主题都要报最佳桥，不管判成什么")
        ok(isinstance(c["block_count"], int), "每个主题都要报子块数")
        ok(isinstance(c["cycle_ratio"], float), "每个主题都要报回路密度")
        if c["best_bridge"]:
            near = c["best_bridge"]["balance"] >= hubs.STITCH_RATIO * (1 - hubs.NEAR_MISS)
            if near and c["grade"] != "疑似两个主题":
                ok(c["near_misses"], "擦边的阴性必须自曝，否则漏报无处可见")
                ok("擦边" in c["flags"], "擦边要进 flags")


@test
def test_tree_topic_says_it_is_a_tree_even_when_stitched():
    """每条边都是桥的团，不能只报「有一处接缝」，那是撒谎。

    回归：等级是单值胜者通吃，「缝合」压过「树」，于是 flags 里算出来的
    「树」从不出现在输出里，读者以为找到了唯一的接缝。
    """
    # 一条 8 点的链，两端各挂一点，保证是树且最均衡的桥切得够平衡。
    members = ["t-%02d" % i for i in range(8)]
    adj = {m: set() for m in members}
    for i in range(7):
        adj[members[i]].add(members[i + 1])
        adj[members[i + 1]].add(members[i])
    adj = {k: sorted(v) for k, v in adj.items()}
    c = hubs._cohesion(members, adj, 7)
    eq(c["cycles"], 0, "链是树")
    ok("树" in c["flags"], "树必须进 flags，哪怕等级被缝合占了")
    ok(c["best_bridge"]["balance"] >= hubs.STITCH_RATIO, "这条链有均衡的桥")
    eq(c["grade"], "疑似两个主题", "等级仍按严重度取缝合")


@test
def test_cohesion_grades_a_tree_and_a_dense_topic_differently():
    """内聚度必须能把「一条链串起来的」和「互相咬合的」分开，否则等级无意义。"""
    # 一条 6 点的链：每条边都是桥，零回路。
    chain = [mk("ch-%02d" % i, ["ch-%02d" % (i + 1)]) for i in range(5)]
    chain.append(mk("ch-05", []))
    # 一个 6 点的稠密团。
    dense = []
    for i in range(6):
        peers = ["dn-%02d" % j for j in range(6) if j != i]
        dense.append(mk("dn-%02d" % i, peers))
    padding = [mk("pad-%02d" % i, ["pad-%02d" % ((i + 1) % 30)]) for i in range(30)]
    result = hubs.compute(chain + dense + padding, today=TODAY)

    tree = topic_of(result, "ch-00")
    knit = topic_of(result, "dn-00")
    eq(tree["cohesion"]["cycles"], 0, "链是树，零回路")
    ok("树" in tree["cohesion"]["flags"], "树形要被标出来")
    ok(knit["cohesion"]["cycles"] > 0, "稠密团有回路")
    eq(knit["cohesion"]["grade"], "实", "稠密团判实")
    ok(tree["cohesion"]["grade"] != "实", "树不能判实")


# --------------------------------------------------------------------------
# render_brief：每主题一行的开场视图
# --------------------------------------------------------------------------


@test
def test_brief_line_budget_and_entries():
    result = hubs.compute(base_corpus(), today=TODAY)
    text = hubs.render_brief(result)
    lines = text.splitlines()
    budget = len(result["topics"]) + 5
    ok(len(lines) <= budget, "brief %d 行，超过主题数 + 5 = %d" % (len(lines), budget))
    for t in result["topics"]:
        if t["named"]:
            ok(t["entry"] in text, "brief 里找不到入口 %s" % t["entry"])
    head = lines[0]
    ok(str(result["notes"]) in head and str(result["edges"]) in head, "头行缺笔记/边总量")


@test
def test_brief_grade_travels_with_each_line():
    import re
    result = hubs.compute(base_corpus(), today=TODAY)
    # 主题行长「%2d. …」；头行也以数字开头（"40 条笔记…"），所以必须认「数字加点」。
    topic_lines = [l for l in hubs.render_brief(result).splitlines()
                   if re.match(r"\s*\d+\. ", l)]
    eq(len(topic_lines), len(result["topics"]), "主题行数与主题数不一致")
    for line in topic_lines:
        ok("[" in line and "]" in line, "主题行缺等级标记：%r" % line)
        ok("窗口新增" in line, "主题行缺窗口新增计数：%r" % line)


@test
def test_brief_fragments_on_one_line():
    result = hubs.compute(base_corpus(), today=TODAY)
    lines = hubs.render_brief(result).splitlines()
    frag = [l for l in lines if l.startswith("碎片")]
    eq(len(frag), 1, "碎片应恰好占一行")
    for f in result["fragments"]:
        for m in f["members"]:
            ok(m in frag[0], "碎片行缺 %s" % m)


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

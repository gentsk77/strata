"""kb frontier 的回归测试。零依赖：python tests/test_frontier.py

两层语料，各测一半契约（docs/derived-views.md §3）：

  - **手搓小图**：测判定规则本身，活跃判定、方向标签阈值、深挖标记、孤儿、
    编辑侧新边 diff。git 侧信号全部注入，不碰任何真 git 史。
  - **样例夹具** `tests/fixtures/sample-corpus/notes/`：18 条编出来的英文笔记
    （与 repo 根的 `notes/` 同一份），event_date 以 2026-08-12 为「今天」排好：
    10 条在窗口外，8 条在窗口内。期望值是 frontier 自身实体（活跃 slug 集合、
    同团 / 异团关系、孤儿清单），不用主题名（那是 hubs 的空间视图实体），也永不
    对活的 notes/ 断言：那里的笔记会被编辑、被新增。
"""

import datetime
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from kb import frontier
from kb.notes import Note, load_all

TODAY = datetime.date(2026, 8, 12)
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "sample-corpus" / "notes"

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


def mk(slug, links=(), event_date="2026-08-10", evidence="stated"):
    fm = {"id": slug, "title": "标题 " + slug, "event_date": event_date, "evidence": evidence}
    body = " ".join("[[%s]]" % t for t in links)
    return Note("notes/%s.md" % slug, fm, body)


def cluster_of(result, slug):
    for c in result["clusters"]:
        if slug in c["members"]:
            return c
    return None


# --------------------------------------------------------------------------
# 手搓小图：判定规则
# --------------------------------------------------------------------------


@test
def test_active_is_event_date_only_when_signals_empty():
    notes = [mk("old-a", event_date="2026-07-01"), mk("new-b", ["old-a"])]
    r = frontier.compute(notes, days=14, today=TODAY)
    eq(r["new_notes"], 1, "只有 new-b 在窗口内")
    eq(r["edited_notes"], 0, "空信号下不该有编辑")
    ok(cluster_of(r, "old-a") is None, "old-a 不该进任何活跃团")


@test
def test_link_to_old_graph_labels_integration_and_doubles_score():
    notes = [mk("old-a", event_date="2026-07-01"), mk("new-b", ["old-a"])]
    r = frontier.compute(notes, days=14, today=TODAY)
    c = cluster_of(r, "new-b")
    eq(c["label"], "整合", "唯一的新边连向旧图，该判整合")
    eq(c["old_link_edges"], 1, "连旧新边 1 条")
    eq(c["score"], 3, "得分 = 新笔记 1 + 2 × 连旧 1")


@test
def test_interlinked_new_notes_label_exploration():
    notes = [mk("new-c", ["new-d"]), mk("new-d", ["new-c"])]
    r = frontier.compute(notes, days=14, today=TODAY)
    c = cluster_of(r, "new-c")
    eq(c["members"], ["new-c", "new-d"], "互链的新笔记该在同一个活跃团")
    eq(c["label"], "探索", "新边全在新笔记之间，该判探索")
    eq(c["score"], 2, "得分 = 新笔记 2 + 2 × 连旧 0")


@test
def test_integration_boundary_includes_equality():
    notes = [
        mk("old-a", event_date="2026-07-01"),
        mk("new-p", ["new-q", "old-a"]),
        mk("new-q", ["new-p"]),
    ]
    r = frontier.compute(notes, days=14, today=TODAY)
    c = cluster_of(r, "new-p")
    eq(c["internal_new_edges"], 1, "内部新边 1")
    eq(c["old_link_edges"], 1, "连旧新边 1")
    eq(c["label"], "整合", "连旧恰好占一半（含相等）该判整合，这是契约的边界条件")


@test
def test_edited_note_with_unchanged_links_gets_no_direction_label():
    notes = [mk("old-f", ["old-a"], event_date="2026-07-01"),
             mk("old-a", event_date="2026-07-01")]
    signals = {"touches": {"old-f": 1}, "links_before": {"old-f": ["old-a"]}}
    r = frontier.compute(notes, days=14, today=TODAY, signals=signals)
    eq(r["edited_notes"], 1, "old-f 该算编辑")
    c = cluster_of(r, "old-f")
    ok(c is not None, "被编辑的笔记该出现在活跃团里")
    eq(c["label"], None, "链接没变 = 没有新增边 = 不给方向标签")


@test
def test_edited_note_new_edge_comes_from_links_before_diff():
    notes = [
        mk("old-g", ["old-h", "old-i"], event_date="2026-07-01"),
        mk("old-h", event_date="2026-07-01"),
        mk("old-i", event_date="2026-07-01"),
    ]
    signals = {"touches": {"old-g": 1}, "links_before": {"old-g": ["old-h"]}}
    r = frontier.compute(notes, days=14, today=TODAY, signals=signals)
    c = cluster_of(r, "old-g")
    eq(c["old_link_edges"], 1, "只有 g→i 是新边（g→h 窗口起点就有）")
    eq(c["label"], "整合", "新边连向旧图")
    eq(c["score"], 2, "得分 = 新笔记 0 + 2 × 连旧 1")


@test
def test_deep_touch_marks_and_repeat_list():
    notes = [mk("old-f", ["old-a"], event_date="2026-07-01"),
             mk("old-a", event_date="2026-07-01")]
    signals = {"touches": {"old-f": 3}, "links_before": {"old-f": ["old-a"]}}
    r = frontier.compute(notes, days=14, today=TODAY, signals=signals)
    c = cluster_of(r, "old-f")
    eq(c["deep_touch"], ["old-f"], "触碰 3 次该标深挖")
    eq(r["repeat_touches"], [{"slug": "old-f", "count": 3}], "反复触碰清单")


@test
def test_orphan_new_note_is_listed_not_clustered():
    notes = [mk("new-lone"), mk("new-c", ["new-d"]), mk("new-d", ["new-c"])]
    r = frontier.compute(notes, days=14, today=TODAY)
    eq(r["orphans"], ["new-lone"], "零度新笔记是孤儿")
    ok(cluster_of(r, "new-lone") is None, "孤儿不占活跃团")


@test
def test_clusters_sorted_by_score():
    notes = [
        mk("old-a", event_date="2026-07-01"),
        mk("pair-a", ["pair-b", "old-a"]), mk("pair-b", ["pair-a"]),
        mk("solo-c", ["solo-d"]), mk("solo-d", ["solo-c"]),
    ]
    r = frontier.compute(notes, days=14, today=TODAY)
    scores = [c["score"] for c in r["clusters"]]
    eq(scores, sorted(scores, reverse=True), "活跃团按得分降序")
    eq(r["clusters"][0]["members"], ["pair-a", "pair-b"], "连旧的团得分高，排前面")


@test
def test_timeline_groups_new_notes_by_date():
    notes = [mk("d1", event_date="2026-08-09"), mk("d2", event_date="2026-08-09"),
             mk("d3", event_date="2026-08-10")]
    r = frontier.compute(notes, days=14, today=TODAY)
    eq([(row["date"], len(row["new"])) for row in r["timeline"]],
       [("2026-08-09", 2), ("2026-08-10", 1)], "时间线按日期分组")


# --------------------------------------------------------------------------
# 样例夹具：18 条编的笔记，期望值按 event_date 与链接机械推出
# --------------------------------------------------------------------------


def _fixture_result():
    notes, errors, _ = load_all(FIXTURE)
    ok(not errors, "夹具语料不该有解析错误：%r" % errors[:2])
    return notes, frontier.compute(notes, days=14, today=TODAY)


@test
def test_fixture_active_set_matches_event_date_oracle():
    notes, r = _fixture_result()
    eq(len(notes), 18, "夹具完整性：18 条笔记")
    cutoff = TODAY - datetime.timedelta(days=14)
    oracle = set()
    for n in notes:
        d = datetime.date.fromisoformat(str(n.fm["event_date"]))
        if d >= cutoff:
            oracle.add(n.slug)
    eq(len(oracle), 8, "夹具完整性：2026-08-12 当天窗口内新建应为 8 条")
    covered = set(r["orphans"])
    for c in r["clusters"]:
        covered.update(c["members"])
    eq(covered, oracle, "活跃团 ∪ 孤儿 必须恰好等于 event_date 窗口内的集合")
    eq(r["new_notes"], 8, "新建计数")
    eq(r["edited_notes"], 0, "空信号下编辑为 0")


@test
def test_fixture_dormant_notes_stay_out():
    _, r = _fixture_result()
    for slug in ("dewey-decimal-classification",
                 "forgetting-curve",
                 "retrieval-is-the-review"):
        ok(cluster_of(r, slug) is None, "%s（窗口外）不该出现在活跃团里" % slug)


@test
def test_fixture_known_pairs():
    _, r = _fixture_result()
    a = cluster_of(r, "zettelkasten")
    b = cluster_of(r, "links-over-folders")
    ok(a is not None and b is not None, "笔记法两条都该活跃")
    ok(a is b, "互链且同窗口的两条该在同一个活跃团")
    eq(a["members"], ["atomic-notes", "commonplace-book", "links-over-folders", "zettelkasten"],
       "笔记法团的成员")
    c = cluster_of(r, "oven-spring")
    ok(c is not None and c is not a, "面包对该自成一团，与笔记法团分开")
    eq(c["members"], ["oven-spring", "why-sourdough-rises"], "面包对的成员")


@test
def test_fixture_cluster_partition_and_labels():
    _, r = _fixture_result()
    shape = sorted(((c["size"], c["label"]) for c in r["clusters"]), reverse=True)
    eq(shape, [(4, "探索"), (2, "整合"), (1, "整合")],
       "活跃团的整体形状（尺寸 + 方向标签）：一个 4 条的探索团、连旧为主的面包对、一条只连旧图的整合单点")
    by_first = {c["members"][0]: c for c in r["clusters"]}
    notes_cluster = by_first["atomic-notes"]
    eq((notes_cluster["internal_new_edges"], notes_cluster["old_link_edges"], notes_cluster["score"]),
       (4, 2, 8), "笔记法团：内部新边 4、连旧 2、得分 4 + 2 × 2")
    bread = by_first["oven-spring"]
    eq((bread["internal_new_edges"], bread["old_link_edges"], bread["score"]),
       (1, 3, 8), "面包对：内部新边 1、连旧 3、得分 2 + 2 × 3")
    eq(by_first["leitner-system"]["score"], 3, "单点：得分 1 + 2 × 1")
    eq([c["members"][0] for c in r["clusters"]], ["atomic-notes", "oven-spring", "leitner-system"],
       "按得分降序，同分时大团在前")


@test
def test_fixture_orphans_and_render_budget():
    _, r = _fixture_result()
    eq(r["orphans"], ["rayleigh-scattering"], "窗口内唯一零度的新笔记是孤儿")
    lines = frontier.render(r).splitlines()
    ok(len(lines) <= 60, "人读输出 %d 行，超过 60 行预算" % len(lines))
    for key in ("timeline", "clusters", "orphans", "repeat_touches"):
        ok(key in r, "JSON 缺表 %s" % key)
    by_first = {c["members"][0]: c for c in r["clusters"]}
    eq(by_first["atomic-notes"]["evidence"], {"revealed": 2, "stated": 1, "inferred": 1},
       "笔记法团的证据构成三档齐全")
    eq(by_first["oven-spring"]["evidence"], {"revealed": 0, "stated": 2, "inferred": 0},
       "面包对全是 stated")


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

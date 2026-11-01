"""kb query: 按一段文字全文检索，返回命中的笔记与它的一跳邻居。

契约见 docs/derived-views.md §4。要点：

  - v0 不做 embedding：检索 = 大小写不敏感的整段子串匹配 + 图遍历。
    先验证结构化检索够不够，不够再上向量。
  - 匹配范围是标题、slug、正文。slug 也算的理由：标题多是中文而 slug 是
    英文，英文检索词经常只有 slug 能接住。
  - 每条命中附一跳邻居（出链、入链都算）。回填横向链接时靠它一眼看出
    「这条周围已经连了谁」，不用再开一次 related。
  - 只读，确定性输出，不落盘。
"""

import re

from . import text as text_util

# 命中排序：标题命中最强（论断本身撞上了检索词），slug 次之，只有正文最弱。
_MATCH_PRIORITY = {"title": 0, "slug": 1, "body": 2}


class EmptyQuery(Exception):
    """空检索词命中所有笔记，那不是一个问题，是一个事故。"""

    def __init__(self):
        super().__init__("检索词是空的。空串会命中所有笔记，那不是检索。")

    def as_json(self):
        return {"error": "empty_query", "reason": "检索词是空的"}


def _neighbors(notes):
    """slug -> (出链集合, 入链集合)。只算两端都在库里的链接，悬空链接不是邻居。"""
    known = {n.slug for n in notes}
    out = {n.slug: set() for n in notes}
    inc = {n.slug: set() for n in notes}
    for n in notes:
        for target in n.links:
            if target in known and target != n.slug:
                out[n.slug].add(target)
                inc[target].add(n.slug)
    return out, inc


def _snippet(body, needle):
    """正文里包含 needle 的那个句子；needle 跨句时退回原文窗口。"""
    s = text_util.sentence_containing(body, needle)
    if s is not None:
        return s
    # 在原文坐标系里找：body.casefold() 对少数字符（ß → ss）会改长度，用它
    # 找到的下标拿去切原文字符串会漂一个字符。re.IGNORECASE 保下标稳定。
    m = re.search(re.escape(needle), body, re.IGNORECASE)
    if m is None:
        return None
    idx = m.start()
    lo, hi = max(0, idx - 40), min(len(body), idx + len(needle) + 40)
    return ("…" if lo else "") + " ".join(body[lo:hi].split()) + ("…" if hi < len(body) else "")


def compute(notes, needle):
    """纯函数：整段 needle 作为一个子串，大小写不敏感地匹配标题、slug、正文。"""
    needle = needle.strip()
    if not needle:
        raise EmptyQuery()
    probe = needle.casefold()

    out_links, in_links = _neighbors(notes)
    by_slug = {n.slug: n for n in notes}

    hits = []
    for n in notes:
        matched = []
        if probe in n.title.casefold():
            matched.append("title")
        if probe in n.slug:  # slug 本来就是全小写 kebab-case
            matched.append("slug")
        body_hit = probe in n.body.casefold()
        if body_hit:
            matched.append("body")
        if not matched:
            continue

        neighbors = []
        for other in sorted(out_links[n.slug] | in_links[n.slug]):
            if other in out_links[n.slug] and other in in_links[n.slug]:
                direction = "both"
            elif other in out_links[n.slug]:
                direction = "out"
            else:
                direction = "in"
            neighbors.append(
                {"slug": other, "title": by_slug[other].title, "direction": direction}
            )

        hits.append({
            "slug": n.slug,
            "title": n.title,
            "matched_in": matched,
            "snippet": _snippet(n.body, needle) if body_hit else None,
            "neighbors": neighbors,
        })

    hits.sort(key=lambda h: (_MATCH_PRIORITY[h["matched_in"][0]], h["slug"]))
    return {
        "query": needle,
        "notes": len(notes),
        "hits": hits,
        "count": len(hits),
    }


def render(result):
    field = {"title": "标题", "slug": "slug", "body": "正文"}
    arrow = {"out": "→", "in": "←", "both": "↔"}
    lines = [
        "命中 %d 条（检索「%s」，共 %d 条笔记）"
        % (result["count"], result["query"], result["notes"])
    ]
    if not result["hits"]:
        return lines[0]
    lines.append("")
    for i, h in enumerate(result["hits"], start=1):
        lines.append(
            "%d. %s  [%s]" % (i, h["title"], "、".join(field[f] for f in h["matched_in"]))
        )
        lines.append("   %s" % h["slug"])
        if h["snippet"]:
            lines.append("   › %s" % h["snippet"])
        if h["neighbors"]:
            lines.append("   一跳邻居 %d 条：" % len(h["neighbors"]))
            for nb in h["neighbors"]:
                lines.append("     %s %s  %s" % (arrow[nb["direction"]], nb["slug"], nb["title"]))
        else:
            lines.append("   孤儿：没有任何一跳邻居")
        lines.append("")
    return "\n".join(lines).rstrip()

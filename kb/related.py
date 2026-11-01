"""kb related: 从一个已知 slug 出发沿链接走 N 跳，看它周围有什么。

契约见 docs/derived-views.md §4。要点：

  - 返回节点（带跳数）、边（诱导子图：走到的节点之间的全部边），
    以及**每条边在正文里写的那句理由**，note-spec 铁律 3 要求每条链接
    带一句为什么，这个命令把那句话找出来还给你。
  - 理由句按边的方向取：a 的正文里挂 [[b]] 的那个句子是 a→b 的理由；
    互链的边两边各有一句。2026-08-11 冷读体检实测 20/20 条链接能抽出理由句，
    抽不出时给 null，不硬造。
  - 只读，确定性输出，不落盘。
"""

from . import text as text_util
from .notes import links_in


class UnknownSlug(Exception):
    """起点不在库里。带上包含该文字的候选 slug，agent 不用再跑一次 query。"""

    def __init__(self, slug, candidates):
        self.slug = slug
        self.candidates = candidates
        hint = "；slug 里包含它的有：%s" % "、".join(candidates) if candidates else ""
        super().__init__("库里没有 %s 这条笔记%s" % (slug, hint))

    def as_json(self):
        return {"error": "unknown_slug", "slug": self.slug, "candidates": self.candidates}


def _adjacency(notes):
    """无向邻接表。只有两端都在库里的链接才成边，悬空链接不是结构。"""
    known = {n.slug for n in notes}
    adj = {slug: set() for slug in known}
    for n in notes:
        for target in n.links:
            if target in known and target != n.slug:
                adj[n.slug].add(target)
                adj[target].add(n.slug)
    return adj


def _reason(note, target):
    """note 正文里挂 [[target]] 的那个句子；抽不出给 None。

    按句子里解析出的链接目标做**精确**匹配，不做子串搜，子串搜 [[aa 会把
    [[aa-longer-slug]] 的句子错认成 aa 的理由。
    """
    for s in text_util.sentences(note.body):
        if target in links_in(s):
            return s
    return None


def compute(notes, origin, hops=1):
    """纯函数：BFS 走 hops 跳，返回节点、诱导子图的边、每条边的理由句。"""
    by_slug = {n.slug: n for n in notes}
    if origin not in by_slug:
        candidates = sorted(s for s in by_slug if origin in s)[:5]
        raise UnknownSlug(origin, candidates)
    if hops < 0:
        raise ValueError("hops 不能是负数")

    adj = _adjacency(notes)

    dist = {origin: 0}
    frontier = [origin]
    for d in range(1, hops + 1):
        nxt = []
        for slug in frontier:
            for nb in sorted(adj[slug]):
                if nb not in dist:
                    dist[nb] = d
                    nxt.append(nb)
        frontier = nxt

    visited = set(dist)
    nodes = [
        {"slug": s, "title": by_slug[s].title, "hops": dist[s]}
        for s in sorted(dist, key=lambda s: (dist[s], s))
    ]

    edges = []
    seen = set()
    for a in sorted(visited):
        for b in sorted(adj[a] & visited):
            key = (min(a, b), max(a, b))
            if key in seen:
                continue
            seen.add(key)
            reasons = []
            for src, dst in ((key[0], key[1]), (key[1], key[0])):
                if dst in by_slug[src].links:
                    reasons.append({"from": src, "sentence": _reason(by_slug[src], dst)})
            edges.append({"a": key[0], "b": key[1], "reasons": reasons})

    return {
        "origin": origin,
        "hops": hops,
        "notes": len(notes),
        "nodes": nodes,
        "edges": edges,
        "counts": {"nodes": len(nodes), "edges": len(edges)},
    }


def render(result):
    lines = [
        "从 %s 走 %d 跳：%d 条笔记，%d 条边"
        % (result["origin"], result["hops"], result["counts"]["nodes"], result["counts"]["edges"])
    ]
    lines.append("")
    for node in result["nodes"]:
        lines.append("%d 跳  %s" % (node["hops"], node["slug"]))
        lines.append("       %s" % node["title"])
    if not result["edges"]:
        lines.append("")
        lines.append("没有边：它是孤儿，或 hops 内没有别的笔记。")
        return "\n".join(lines).rstrip()
    lines.append("")
    lines.append("边（每条附正文里写的理由句）：")
    for e in result["edges"]:
        lines.append("  %s ↔ %s" % (e["a"], e["b"]))
        for r in e["reasons"]:
            if r["sentence"]:
                lines.append("    %s：%s" % (r["from"], r["sentence"]))
            else:
                lines.append("    %s：（正文里抽不出理由句，链接可能挂在表格或别的结构里）" % r["from"])
    return "\n".join(lines).rstrip()

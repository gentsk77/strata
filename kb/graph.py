"""Graph primitives for the derived views, plus the `kb graph` view itself.

两层，分界线在文件中部那道横线上：

  - **上半：纯图论原语。** 不装 strata 的语义，只认 (nodes, adj)。`hubs` 用它
    算主题结构，未来的 `frontier`、`related` 用同一份。
  - **下半：`kb graph` 这条命令的视图。** 它得认笔记，所以这是本文件里唯一
    碰 strata 语义的地方。它不自己解析链接、也不自己找理由句，邻接表和
    理由句都问 `related` 要，那边是这两件事的唯一实现（`kb related` 的契约见
    kb/related.py 的模块 docstring）。同一条链接在两个命令里必须给出同一句
    「为什么」，各写一份就必然漂移。

全部确定性：任何返回值都排过序，不依赖 set/dict 的迭代顺序。
"""

from . import related


def components(nodes, adj):
    """连通分量，每个是排序好的 slug 列表；分量之间按大小降序、首元素升序。"""
    seen, out = set(), []
    for start in sorted(nodes):
        if start in seen:
            continue
        stack, group = [start], []
        seen.add(start)
        while stack:
            cur = stack.pop()
            group.append(cur)
            for nxt in sorted(adj.get(cur, ())):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        out.append(sorted(group))
    out.sort(key=lambda g: (-len(g), g[0]))
    return out


def bridges(nodes, adj):
    """桥边：删掉它图就断成两块。返回排序好的 (a, b) 列表，a < b。

    迭代版 Tarjan。递归版在 135 个节点上够用，但这是给知识库长期用的，
    深度只跟最长路径有关，不值得赌。
    """
    disc, low, parent = {}, {}, {}
    found, timer = [], [0]

    for root in sorted(nodes):
        if root in disc:
            continue
        # 栈帧 = (节点, 待访问邻居的迭代器)
        disc[root] = low[root] = timer[0]
        timer[0] += 1
        stack = [(root, iter(sorted(adj.get(root, ()))))]
        while stack:
            node, it = stack[-1]
            advanced = False
            for nxt in it:
                if nxt == parent.get(node):
                    # 只跳过回到父节点的第一条边；平行边在本项目里不存在
                    # （build_graph 已经去重），所以直接跳过是安全的。
                    continue
                if nxt not in disc:
                    parent[nxt] = node
                    disc[nxt] = low[nxt] = timer[0]
                    timer[0] += 1
                    stack.append((nxt, iter(sorted(adj.get(nxt, ())))))
                    advanced = True
                    break
                low[node] = min(low[node], disc[nxt])
            if advanced:
                continue
            stack.pop()
            if stack:
                up = stack[-1][0]
                low[up] = min(low[up], low[node])
                if low[node] > disc[up]:
                    found.append((min(up, node), max(up, node)))
    return sorted(found)


def betweenness(nodes, adj):
    """Brandes 无权介数中心性。slug -> 分数。

    用途是打破入口选择的平局：入链数相同时，处在更多条最短路径上的那条
    更像这个主题的枢纽，而按 slug 字母序选纯粹是任意的。
    """
    score = {v: 0.0 for v in nodes}
    for s in sorted(nodes):
        stack, preds = [], {v: [] for v in nodes}
        sigma = {v: 0.0 for v in nodes}
        dist = {v: -1 for v in nodes}
        sigma[s], dist[s] = 1.0, 0
        queue = [s]
        head = 0
        while head < len(queue):
            v = queue[head]
            head += 1
            stack.append(v)
            for w in sorted(adj.get(v, ())):
                if dist[w] < 0:
                    dist[w] = dist[v] + 1
                    queue.append(w)
                if dist[w] == dist[v] + 1:
                    sigma[w] += sigma[v]
                    preds[w].append(v)
        delta = {v: 0.0 for v in nodes}
        while stack:
            w = stack.pop()
            for v in preds[w]:
                delta[v] += (sigma[v] / sigma[w]) * (1.0 + delta[w])
            if w != s:
                score[w] += delta[w]
    # 无向图每对最短路被数了两次
    return {v: score[v] / 2.0 for v in nodes}


def subgraph(members, adj):
    """把 adj 限制到 members 内部，返回 (member_set, 内部邻接, 内部边数)。"""
    member_set = set(members)
    sub = {m: sorted(set(adj.get(m, ())) & member_set) for m in members}
    edge_count = sum(len(v) for v in sub.values()) // 2
    return member_set, sub, edge_count


def split_sizes(members, sub_adj, drop_edge):
    """删掉一条边之后，两块各有多大。返回排序好的 (小块, 大块)。"""
    a, b = drop_edge
    seen, stack = {members[0]}, [members[0]]
    while stack:
        cur = stack.pop()
        for nxt in sub_adj.get(cur, ()):
            if (cur, nxt) == (a, b) or (cur, nxt) == (b, a):
                continue
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    first = len(seen)
    return tuple(sorted((first, len(members) - first)))


# --------------------------------------------------------------------------
# `kb graph` 的视图。以下是本文件唯一认识「笔记」的部分。
# --------------------------------------------------------------------------

# 人读输出里列几个度数最高的节点。JSON 从不截断，这个数只影响皮肤。
TOP_N = 8


def _pick_reason(by_slug, a, b):
    """无向边 (a, b) 的那句理由，以及它抄自谁的正文。抽不出给 (None, None)。

    一条边最多有两句理由（互链时两边各写一句），而这个视图每条边只给一句，
    审理屏的图栏一条边只有一个位置放字。取谁按固定顺序：先 a 侧，a 侧抽不出
    才退到 b 侧，两侧都抽不出就是 None。**不硬造**，理由同 kb/related.py：
    编一句出来的代价是维护者以为正文里真写了这句话。

    附 reason_from 是因为「这句话是谁写的」不能靠猜：互链时 b 侧那句读起来
    像是在讲 a，把它挂在 a 名下会张冠李戴。
    """
    for src, dst in ((a, b), (b, a)):
        if dst in by_slug[src].links:
            sentence = related._reason(by_slug[src], dst)
            if sentence is not None:
                return sentence, src
    return None, None


def compute(notes):
    """纯函数：整库的图。节点带 degree，边带两个端点与那句理由。

    邻接表问 `related` 要，于是「什么算一条边」这条规则只有一份：两端都在库里
    才成边，悬空链接与自环不算，重复链接只算一次，方向抹平。`kb graph` 与
    `kb related` 因此不可能对同一个库给出两张不同的图。
    """
    by_slug = {n.slug: n for n in notes}
    adj = related._adjacency(notes)

    nodes = [
        {
            "slug": slug,
            "title": by_slug[slug].title,
            "status": by_slug[slug].status,
            "evidence": by_slug[slug].evidence,
            "degree": len(adj[slug]),
        }
        for slug in sorted(by_slug)
    ]

    # 每对只出一次，source < target（字典序）。这就是去重与无向归一：
    # a→b 和 b→a 落在同一个 (min, max) 上，第二次遇到时被 b < a 挡掉。
    edges = []
    for a in sorted(adj):
        for b in sorted(adj[a]):
            if b < a:
                continue
            reason, reason_from = _pick_reason(by_slug, a, b)
            edges.append(
                {"source": a, "target": b, "reason": reason, "reason_from": reason_from}
            )

    return {
        "nodes": nodes,
        "edges": edges,
        "counts": {"nodes": len(nodes), "edges": len(edges)},
    }


def render(result):
    nodes, edges = result["nodes"], result["edges"]
    if not nodes:
        return "库是空的"

    lines = ["%d 个节点，%d 条边" % (result["counts"]["nodes"], result["counts"]["edges"])]
    orphans = [n for n in nodes if n["degree"] == 0]
    if orphans:
        lines.append("其中 %d 条谁也没连上" % len(orphans))
    mute = [e for e in edges if e["reason"] is None]
    if mute:
        lines.append("%d 条边在正文里抽不出理由句" % len(mute))

    top = sorted(nodes, key=lambda n: (-n["degree"], n["slug"]))[:TOP_N]
    if top and top[0]["degree"]:
        lines.append("")
        lines.append("连得最多的 %d 条：" % len(top))
        for n in top:
            lines.append("  %2d 条边  %s" % (n["degree"], n["slug"]))
            lines.append("            %s" % n["title"])
    return "\n".join(lines).rstrip()

"""kb watch: 维护 `kb ingest` 的来源清单。契约见 docs/kb-tool.md §3。

一句话：watch 记住「去哪几个 repo 捞」，ingest 负责捞。watch 不读 repo 内容、
不写 inbox/，它只管一份清单。

行为钉死：

  - 清单住 **repo 根的 `watchlist.json`，入 git**。理由：它是维护者做的一个
    选择，不是可重算的派生数据，所以不能住 `.kb/`（那个目录随时可删）。
  - 条目是 **workspace 相对的目录名**（例：`learning-agent`），不是绝对路径。
    绝对路径换台机器就失效，而 workspace 相对已经是 artifact 指针的既有约定
    （doctor N6 同款解析）。
  - 只收 workspace 相对名，**缩小**（不是关掉）ingest 的一个已知模糊性：ingest
    的游标键是 repo 目录名，两个同名目录的 repo 会共享游标、互相覆盖进度。清单
    这条路上同名即同一个 repo，撞不了车；但 `kb ingest <路径>` 这条一次性覆盖
    的路仍然按目录名记游标，拿一个同名的别处 repo 跑，会顶掉被 watch 的那个的
    进度，并覆盖它在 inbox 里的复印件。彻底修要改 ingest 的游标键，不在本文件。
  - **加之前验它是 git repo 根**：ingest 的 `git ls-tree` 清单以 repo 根为基准，
    指到子目录会让路径错位。与 ingest 的同款检查对齐，早报错好过捞错东西。
  - 加已在清单里的条目 = 无操作，不报错（幂等）。
  - 删不在清单里的条目 = 报错并列出现有条目，因为那多半是打错了名字。
"""

import json
import os
import subprocess
from pathlib import Path

WATCHLIST_NAME = "watchlist.json"


class WatchError(Exception):
    """清单操作失败。带结构化形态，理由同 hubs/query：agent 要能问出为什么。"""

    def __init__(self, kind, reason, **extra):
        super().__init__(reason)
        self.kind = kind
        self.reason = reason
        self.extra = extra

    def as_json(self):
        payload = {"error": self.kind, "reason": self.reason}
        payload.update(self.extra)
        return payload


def path_of(root):
    return Path(root) / WATCHLIST_NAME


def load(root):
    """读清单。**没有文件 = 空清单；有文件但读不懂 = 报错，不当空清单。**

    这两者必须分开：当成空清单的话，下一次 `add` 会把「空清单加一条」原样写回，
    一个五条的清单就此变成一条，而 `kb watch` 会平静地列出那一条、退出 0，一次
    静默的数据丢失。宁可让当事会话看见 corrupt_watchlist 去 git 里捞回来。
    """
    p = path_of(root)
    if not p.exists():
        return []
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as e:
        raise WatchError("unreadable_watchlist", "清单读不出来：%s" % e, path=str(p))
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise WatchError("corrupt_watchlist", "清单不是合法 JSON：%s" % e, path=str(p))
    if not isinstance(data, dict) or not isinstance(data.get("watched"), list):
        raise WatchError(
            "corrupt_watchlist", "清单的形状不对，应是 {\"watched\": [名字…]}", path=str(p)
        )
    names = []
    for n in data["watched"]:
        if not isinstance(n, str) or not n.strip():
            raise WatchError("corrupt_watchlist", "清单里有一条不是字符串：%r" % (n,), path=str(p))
        # 入口验一次不够：手改文件、或别的写入方，都能绕过 add 的检查把非法名字
        # 塞进来。读的这一端不验，非法条目会一路走到 resolve 才炸。
        _check_name(n)
        names.append(n)
    return names


def save(root, names):
    """先写临时文件再原子替换：写到一半断电不会留下一个半截的清单。

    写不进去（只读、没权限、盘满）要包成 WatchError，并且**把临时文件收干净**，
    裸 OSError 会绕过调用方的 except 变成 traceback，而留下的 .tmp 会当作未跟踪
    文件出现在一个「git 历史必须是纯知识时间线」的 repo 根上。
    """
    p = path_of(root)
    payload = {"watched": sorted(set(names))}
    tmp = p.with_name(p.name + ".tmp")
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(str(tmp), str(p))
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise WatchError("unwritable_watchlist", "清单写不进去：%s" % e, path=str(p))


def _is_git_root(path):
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(path), capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if out.returncode != 0:
        return False
    top = Path(out.stdout.strip())
    return os.path.normcase(str(top.resolve())) == os.path.normcase(str(Path(path).resolve()))


def _check_name(name):
    """条目必须是一个纯目录名。

    只挡分隔符不够：`.` 与 `..` 不含分隔符却指向 workspace 自己和它的父目录，
    Windows 上 `D:` 更是直接跳到另一个盘。这些都会被原样存进一个入 git、跨机器
    共享的文件里，在别人机器上指向完全不同的地方。
    """
    if name in (".", ".."):
        raise WatchError("not_a_name", "条目不能是 . 或 ..：%s" % name, name=name)
    if name != name.strip():
        # Windows 会把结尾空格吃掉，于是 "alpha " 和 "alpha" 指同一个目录却是
        # 两个条目：同一份原料复印两遍进 inbox，游标也长出两个键。
        raise WatchError("not_a_name", "条目首尾不能有空白：%r" % name, name=name)
    if Path(name).parts != (name,) or os.path.splitdrive(name)[0]:
        raise WatchError(
            "not_a_name", "条目必须是 workspace 下的目录名，不是路径：%s" % name, name=name
        )


def resolve(workspace, name):
    """条目名 → 绝对路径。"""
    _check_name(name)
    return Path(workspace) / name


def add(root, workspace, names):
    """加条目。逐条验存在、验 git 根、验重名，任何一条不过就整批不落盘。"""
    current = load(root)
    added = []
    for name in names:
        target = resolve(workspace, name)
        if name in current or name in added:
            continue
        if not target.is_dir():
            raise WatchError(
                "not_found", "workspace 下没有这个目录：%s" % name, name=name,
                looked_at=str(target),
            )
        if not _is_git_root(target):
            raise WatchError(
                "not_a_git_root", "不是 git repo 的根目录：%s" % name, name=name,
                looked_at=str(target),
            )
        added.append(name)
    if added:
        save(root, current + added)
    return {"added": added, "watched": load(root)}


def remove(root, names):
    current = load(root)
    missing = [n for n in names if n not in current]
    if missing:
        # 现有条目要进人读的那句话，不能只进 JSON：打错名字的人正需要看见真名，
        # 而 kb-tool.md §3 承诺的「列出现有清单」没有限定只在 --json 下成立。
        have = "、".join(current) if current else "（空）"
        raise WatchError(
            "not_watched",
            "清单里没有这些条目：%s。现有条目：%s" % ("、".join(missing), have),
            missing=missing, watched=current,
        )
    kept = [n for n in current if n not in names]
    save(root, kept)
    return {"removed": list(names), "watched": kept}


def status(root, workspace):
    """清单加每条的当下可达性。ingest 之前想知道「清单还准不准」时用。"""
    entries = []
    for name in load(root):
        target = Path(workspace) / name
        entries.append({
            "name": name,
            "path": str(target),
            "exists": target.is_dir(),
            "git_root": target.is_dir() and _is_git_root(target),
        })
    return {"watched": entries, "count": len(entries)}


def render(result):
    entries = result["watched"]
    if not entries:
        return "watch 清单是空的。用 `kb watch add <目录名>` 加来源。"
    lines = ["watched %d 个 repo：" % len(entries)]
    for e in entries:
        if not e["exists"]:
            mark = "  [找不到]"
        elif not e["git_root"]:
            mark = "  [不是 git repo 根]"
        else:
            mark = ""
        lines.append("  %s%s" % (e["name"], mark))
    return "\n".join(lines)

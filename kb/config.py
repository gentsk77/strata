"""kb 的路径解析：工具住 strata-bench，笔记住 strata。

2026-08-13 之前 kb 与 `notes/` 同住 strata 一个 repo，所以「知识库在哪」不用问，
从 cwd 往上找到 `notes/` 就是它。工具搬进 strata-bench 之后这条默认值永远落空
（bench 没有 `notes/`），「知识库在哪」于是变成一个必须显式配置的事实。理由与被
否决的方案见 `docs/decisions.md` 的 D3。

**两个根，不是一个。** 迁移把原本重合的两件事拆开了：

  - **notes 根**：strata 的 checkout。`notes/`、`inbox/`、`.kb/` 住这里。
  - **工具根**：bench 的 checkout，即 kb 包的上一级目录。`watchlist.json` 与
    `strata.config.json` 住这里。

notes 根的解析顺序，先命中先用：

  1. **`--root`**：把该目录当成一个**自包含**的 kb 工作区，`notes/` 与
     `watchlist.json` 都从它下面取。这是迁移前 `--root` 的原样语义（那时两个根
     本来就是同一个目录），全部测试夹具依赖它，所以不动。
  2. 环境变量 `STRATA_ROOT`。
  3. 配置文件 `<工具根>/strata.config.json` 的 `strataRoot` 字段。**相对路径按
     bench 的主 checkout 解析**（见 `main_checkout()`），所以它可以写成 `../strata`
     并入 git：不含任何机器专属信息，换台机器照样对。
  4. 兄弟目录默认值 `<bench 主 checkout>/../strata`，且它得真的有 `notes/`。这是
     bench 与 strata 在 workspace 下互为兄弟目录的既定布局，零配置就能跑。
  5. 都没命中 → 抛 ConfigError。

**第 5 条不许退化成「当前目录」。** 迁移前的兜底是「找不到就返回 cwd」，搬家之后
那等于让 `kb doctor` 在一个没有笔记的目录里平静地打印「检查了 0 条笔记，违规 0 条，
全绿」并退出 0。一个绿色的空结果比一条红色的错误危险得多：调用方（app 的门禁、
skill 的收尾检查）读到的是「库是干净的」，而不是「我根本没找到库」。

2 到 4 解析出来的路径一律验 `notes/` 存在，不存在就报错；`--root` 不验，理由见
`resolve()` 的注释。
"""

import json
import os
import subprocess
from pathlib import Path

CONFIG_NAME = "strata.config.json"
CONFIG_KEY = "strataRoot"
ENV_VAR = "STRATA_ROOT"
SIBLING_NAME = "strata"


class ConfigError(Exception):
    """路径解析失败。带结构化形态，理由同 watch/hubs：agent 要能问出为什么。"""

    def __init__(self, kind, reason, **extra):
        super().__init__(reason)
        self.kind = kind
        self.reason = reason
        self.extra = extra

    def as_json(self):
        payload = {"error": self.kind, "reason": self.reason}
        payload.update(self.extra)
        return payload


class Roots:
    """一次解析的结果。`source` 是给人看的「这个答案哪来的」。"""

    def __init__(self, notes_root, config_root, source):
        self.notes_root = Path(notes_root)
        self.config_root = Path(config_root)
        self.source = source

    @property
    def notes_dir(self):
        return self.notes_root / "notes"

    @property
    def inbox_dir(self):
        return self.notes_root / "inbox"

    @property
    def cursor_file(self):
        return self.notes_root / ".kb" / "ingest-cursor.json"

    @property
    def watchlist_file(self):
        return self.config_root / "watchlist.json"

    def as_json(self):
        return {
            "strata_root": str(self.notes_root),
            "notes": str(self.notes_dir),
            "inbox": str(self.inbox_dir),
            "ingest_cursor": str(self.cursor_file),
            "bench_root": str(self.config_root),
            "watchlist": str(self.watchlist_file),
            "source": self.source,
        }


def bench_root():
    """kb 包的上一级：**这一份** checkout 的根。

    `watchlist.json` 与 `strata.config.json` 都住这里，两者都入 git，所以每个
    worktree 自带一份自己的副本，这正是想要的：worktree 里改清单不该影响主
    checkout 的那一份。

    不做成可配置的：它是「这份代码在哪」，问文件系统就有确定答案，多一个环境变量
    只会多一处能和现实不一致的地方。
    """
    return Path(__file__).resolve().parent.parent


def main_checkout(start=None):
    """bench 的**主** checkout。在 git worktree 里跑时，它不等于 bench_root()。

    worktree 的根是 `<bench>/.claude/worktrees/<名字>`，它的父目录是 `worktrees/`
    而不是 workspace。所以「兄弟目录 strata」与配置里的相对路径都不能按 worktree
    自己的位置算，否则每开一条并行工作线就要重配一次路径，而「配起来麻烦」正是
    绝对路径被写死进代码的成因（strata-companion 的旧版就是这么来的）。

    修法：问 git 主 repo 的 `.git` 在哪（`--git-common-dir` 在 worktree 里指回主
    repo 的 `.git`），据此上溯。与 doctor._workspace_root 同一条技术、同一条理由，
    那边是 2026-08-12 被 52 条误报逼出来的。git 不可用或不在 repo 里就退回这一份
    checkout 自己，与没有 worktree 时行为一致。
    """
    root = Path(start or bench_root()).resolve()
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(root), capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return root
    common = out.stdout.strip()
    if out.returncode != 0 or not common:
        return root
    common_path = Path(common)
    if not common_path.is_absolute():
        common_path = root / common_path
    return common_path.resolve().parent


def config_path():
    return bench_root() / CONFIG_NAME


def _from_env():
    raw = os.environ.get(ENV_VAR, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def _from_config_file(bench):
    p = bench / CONFIG_NAME
    if not p.exists():
        return None
    try:
        # utf-8-sig 而不是 utf-8：这是一份维护者会手改的配置文件，而 Windows 上一
        # 串常用写法（PowerShell 的 `Out-File -Encoding utf8`、记事本另存为 UTF-8）
        # 都会加 BOM。用 utf-8 读的话，一个内容完全正确的文件会报 "Unexpected
        # UTF-8 BOM"，而屏幕上肉眼看不出任何毛病。utf-8-sig 有 BOM 就吃掉、没有就
        # 当普通 utf-8，是严格更宽的一侧。
        raw = p.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise ConfigError("unreadable_config", "配置文件读不出来：%s" % e, path=str(p))
    try:
        data = json.loads(raw)
    except ValueError as e:
        # 与 watch.load 同一条姿态：读不懂**不当作没配置**。当作没配置的话，解析
        # 会静静滑到兄弟目录默认值上，于是一个打错字的配置文件表现为「配置没生效」
        # 而不是「配置坏了」，而这两者的修法完全不同。
        raise ConfigError("corrupt_config", "配置文件不是合法 JSON：%s" % e, path=str(p))
    if not isinstance(data, dict):
        raise ConfigError(
            "corrupt_config", '配置文件的形状不对，应是 {"%s": "路径"}' % CONFIG_KEY, path=str(p)
        )
    value = data.get(CONFIG_KEY)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(
            "corrupt_config", "%s 要是一个非空字符串，收到 %r" % (CONFIG_KEY, value), path=str(p)
        )
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        # 相对路径按 **bench 的主 checkout** 解析，既不按 cwd 也不按本 worktree：
        # 按 cwd 解析会让同一份配置随调用者站在哪儿指向不同地方；按 worktree 解析
        # 会让 `../strata` 在 worktree 里指向 `.claude/worktrees/strata`。配置文件
        # 入 git，跨 worktree 共享，所以它的相对基准必须也是跨 worktree 稳定的。
        path = main_checkout() / path
    return path


def _sibling_default():
    """bench 与 strata 在 workspace 下互为兄弟目录，这是既定布局，零配置就该能跑。"""
    candidate = main_checkout().parent / SIBLING_NAME
    return candidate if (candidate / "notes").is_dir() else None


def resolve(explicit_root=None):
    """→ Roots。顺序见模块头。找不到抛 ConfigError。"""
    bench = bench_root()

    if explicit_root:
        # --root **不验** notes/ 是否存在，与迁移前一字不差：它是「我知道我在干
        # 什么」的逃生口（测试夹具、临时语料、别人的 kb 工作区），验了会把一批
        # 现成的用法判死。代价是打错的 --root 仍然静默给出 0 条笔记，那是迁移
        # 前就有的行为，本次不扩大也不修，修它要单独一条裁决。
        root = Path(explicit_root).expanduser()
        return Roots(root, root, "--root")

    tried = []

    env_root = _from_env()
    if env_root is not None:
        tried.append("%s=%s" % (ENV_VAR, env_root))
        return Roots(_require_notes(env_root, "环境变量 %s" % ENV_VAR), bench, ENV_VAR)
    tried.append("环境变量 %s（未设置）" % ENV_VAR)

    file_root = _from_config_file(bench)
    if file_root is not None:
        tried.append("%s 的 %s=%s" % (CONFIG_NAME, CONFIG_KEY, file_root))
        return Roots(
            _require_notes(file_root, "%s 的 %s" % (bench / CONFIG_NAME, CONFIG_KEY)),
            bench,
            CONFIG_NAME,
        )
    tried.append("配置文件 %s（不存在或没有 %s 字段）" % (bench / CONFIG_NAME, CONFIG_KEY))

    sibling = _sibling_default()
    if sibling is not None:
        return Roots(sibling.resolve(), bench, "兄弟目录默认值")
    tried.append(
        "兄弟目录默认值 %s（不存在，或它下面没有 notes/）" % (main_checkout().parent / SIBLING_NAME)
    )

    raise ConfigError(
        "strata_root_not_found",
        "找不到 strata 的 checkout。按顺序试过：%s。修法：在 %s 里写 "
        '{"%s": "."}（笔记就在本 repo 的 notes/ 时），或设环境变量 %s，或显式传 --root。'
        % ("；".join(tried), bench / CONFIG_NAME, CONFIG_KEY, ENV_VAR),
        tried=tried,
        config_file=str(bench / CONFIG_NAME),
        config_key=CONFIG_KEY,
        env_var=ENV_VAR,
    )


def _require_notes(root, source_desc):
    """显式配置出来的根必须真的是知识库根，否则当场报错。

    只对配置来源生效，不对 --root 生效。配置是「一次写好、天天用」的东西，写错
    了会在之后每一次运行里静默产出空结果；逃生口是「这一次我自己负责」。
    """
    root = Path(root).expanduser()
    if not (root / "notes").is_dir():
        raise ConfigError(
            "notes_dir_missing",
            "%s 指到 %s，但那里没有 notes/ 目录" % (source_desc, root),
            configured=str(root),
            looked_for=str(root / "notes"),
            source=source_desc,
        )
    return root.resolve()

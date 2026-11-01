"""跑 tests/ 下全部 kb 测试：python tests/run_all.py

每个 test_*.py 都是一个独立的零依赖脚本，退出码即结果。分开子进程跑，不 import
进同一个解释器：test_config.py 会临时替换 kb.config 的模块级函数，同进程跑会污染
别的用例。

2026-08-13 随 kb 从 strata 迁入时新增，那边没有 runner，八个文件靠手敲，而
「手敲八条命令」这件事本身就是测试不被跑的常见原因。
"""

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 子进程一律用 utf-8 写 stdout。多数套件自己在 main() 里 reconfigure 过，但不是
# 全部，漏掉的那些在管道下会退回本机 locale（Windows 上是 GBK），于是 runner
# 按 utf-8 去读就得到一行乱码；更坏的是 GBK 的 0xC8AB 正好是合法 utf-8（U+022B
# 'ȫ'），所以乱码解得出来、不报错，只是转印回 GBK 控制台时炸 UnicodeEncodeError。
# 在这里统一钉死，比指望十个文件各自记得那两行可靠。
CHILD_ENV = dict(os.environ, PYTHONIOENCODING="utf-8")


def main():
    # runner 自己也得转，否则子进程摘要里的中文照样印不出去。kb/cli.py 同款两行。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    # 套件靠 glob 自动发现，新增一个 test_*.py 就自动进这一轮，不用登记。
    suites = sorted(HERE.glob("test_*.py"))
    if not suites:
        print("tests/ 下没有 test_*.py", file=sys.stderr)
        return 1

    failed = []
    for suite in suites:
        proc = subprocess.run(
            [sys.executable, str(suite)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=CHILD_ENV,
        )
        tail = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
        summary = tail[-1] if tail else "(无输出)"
        mark = "ok  " if proc.returncode == 0 else "FAIL"
        print("%s %-28s %s" % (mark, suite.name, summary))
        if proc.returncode != 0:
            failed.append((suite.name, proc.stdout, proc.stderr))

    print("")
    if failed:
        for name, out, err in failed:
            print("---- %s ----" % name)
            print(out.rstrip())
            if err.strip():
                print(err.rstrip())
        print("失败 %d / %d 个套件" % (len(failed), len(suites)))
        return 1
    print("全部通过：%d 个套件" % len(suites))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""novel 子包 — 小说线实现（structured-writer 小说模式）"""
import sys
from pathlib import Path

# 从 novel-weaver 移植的脚本使用绝对导入（from _path_utils import ... / from novel_xxx import ...），
# 进程内被 novel_bridge import 时，必须先把这个目录加入 sys.path，否则报
# No module named '_path_utils'。子进程调用（cwd=本目录）不受影响。
_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

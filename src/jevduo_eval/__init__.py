"""jevduo_eval：集群测评薄适配层。

worker 侧 6 文件：entry/env/runner/recorder/scoring/manifest（计划 §五）；
rjob.py 是开发机侧生命周期客户端（第 7 个模块，偏离"6 文件封顶"已记入 DEVIATIONS.md）。
"""

from .entry import entry_main, run_from_spec

__all__ = ["entry_main", "run_from_spec"]

"""反作弊闸门。每道闸门问同一个问题：**这一轮 worker 动了不该动的东西吗**。

## 为什么单独成包

原先十四道闸门顺序写在 `Dispatcher._review` 里，那个方法 462 行。搬出来
不是因为行数，是因为这些闸门有三条谁都能看见的共同结构，写在一个方法里
看不出来：

  1. 判据一律是**差分**，不是现状。「有没有 pre-commit」是错的判据（合法
     仓库装了 lint hook 就恒红），「这一轮 pre-commit 变了吗」才是对的。
  2. 所以每道闸门都要一份**派发前的基线**，而基线必须在轮次循环**之外**
     取一次。取在循环里实测会漏：第一轮 worker 写了 hook → 判红打回 →
     第二轮开头重取基线，那个 hook 已经在里面了 → 差异为空 → 合并。
  3. 拦下来的产物形状一致：(闸门名, 用什么量的, 期望, 实际)。

## 顺序是语义，不是排版

`ORDER` 里的次序是实测教训，不能按字母排、不能「看起来更整齐」地重排：

  - `git-hook-touched` 必须最前 —— 只有它的影响范围**在本 workspace 之外**
    （`.git/hooks` 在 common dir，父仓库和所有并行 worktree 共用一份）。
  - `shadow-code` / `runner-hook-added` / `index-skip-flag` 必须在跑 check
    **之前** —— 它们污染的正是那份绿。拦下来之后再跑 check 只是在一个已知
    被污染的树上多花一次钱。
  - `git-config-touched` 必须在两道 diff 闸门之前 —— `diff.external` 和
    `core.attributesFile` 能让那两道自己失效。

## 加一道新闸门要做的事

  1. 在 `specs.py` 里加一个 `GateSpec`，写清 probe / baseline / 判据；
  2. 把名字加进 `ORDER`，位置按上面三条约束想清楚；
  3. 在 `factory/gate_claims.py` 的 `GATE_CLAIMS` 里加说明文字。

第 3 步不是可选的：`tests/test_gate_visibility.py` 双向断言「每个闸门名都
有说明」且「表里没有废弃名字」，漏了会红。
"""

from __future__ import annotations

from factory.gates.specs import (
    ORDER,
    GateSpec,
    Snapshot,
    baseline,
    evaluate,
    gate_by_name,
)

__all__ = [
    "ORDER",
    "GateSpec",
    "Snapshot",
    "baseline",
    "evaluate",
    "gate_by_name",
]

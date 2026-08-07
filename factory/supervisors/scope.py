"""范围监工：改动有没有跑出任务声明的范围。确定性，零模型调用。

分级引擎回答的是「改动落在哪里危险不危险」，它管不到「改动落在哪里算离题」。
实测过的漏洞：一个任务声明只改 src/util/text.py，worker 顺手改了 docs/readme.md
和 src/util/other.py —— 三个文件都不匹配任何分级规则，后分级仍然是 A 类，
干净合并。分级引擎没有错，这不是它的问题域。

为什么这件事值得单独拦：

  - 无人工厂里没人看 diff。一次「顺手改的」在有人 review 的流程里会被问一句，
    在无人流程里直接进主干。攒够几十次之后，主干上有一批没人记得为什么存在的
    改动，而审计记录会告诉你每一条都全绿合并过。
  - 越界改动是**上游需求和实际产出不一致**的最便宜的证据。规格监工能查语义
     一致性，但它要调模型、要花钱，而且只有 criteria 非空才有意义。路径对不上
    连模型都不用调。

**declared_paths 为空 = 没有范围约定，一律 PASS。** 这不是偷懒：
`factory/intake/extract.py` 明确要求「用户没说文件名就留空数组」，口述来源的
任务绝大多数因此为空。空数组当成「什么都不许改」会让几乎每个任务都被打回，
而打回理由是任务自己没写清楚 —— worker 修不了这个，三轮全烧完然后上人。
要范围约束就显式写 declared_paths，这和 runbook 不默认加载是同一个取舍。
"""

from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatch

from factory.audit.models import SupervisorRole, Verdict
from factory.supervisors.base import SupervisorReport

# 越界文件列进 claim 的上限。worker 要的是「改回哪几个」，
# 糊 200 行路径进 prompt 只会挤掉真正的失败项。
_MAX_LISTED = 20


def out_of_scope(
    changed: Iterable[str], declared: Iterable[str]
) -> tuple[str, ...]:
    """算出越界的文件，保序去重。

    declared 里的条目按 fnmatch 匹配，和分级规则用同一套语义 —— 声明
    `src/util/*.py` 和分级规则里写 `src/util/*.py` 应该匹配同一批文件，
    两处用不同的匹配规则是纯粹的坑。

    目录形式的声明（`docs/` 或 `docs`）当成 `docs/**` 处理：写 declared_paths
    的人（或抽取器）最自然的写法是目录名，而 fnmatch 的 `*` 不跨 `/`，
    不特殊处理的话 `docs/` 匹配不上 `docs/readme.md`，声明了反而全部越界。
    """
    pats: list[str] = []
    for d in declared:
        d = str(d).strip()
        if not d:
            continue
        pats.append(d)
        if d.endswith("/"):
            pats.append(d + "**")
        elif not any(ch in d for ch in "*?["):
            # 既可能是文件也可能是目录，两种都给一条模式，代价只是多一次匹配
            pats.append(d.rstrip("/") + "/**")

    hits: list[str] = []
    for path in changed:
        p = str(path)
        if any(fnmatch(p, pat) for pat in pats):
            continue
        hits.append(p)
    return tuple(dict.fromkeys(hits))


class ScopeSupervisor:
    """比对 changed_paths 和 declared_paths。"""

    role = SupervisorRole.SCOPE

    def review(
        self, *, changed_paths: Iterable[str], declared_paths: Iterable[str]
    ) -> SupervisorReport:
        declared = tuple(str(d) for d in declared_paths if str(d).strip())
        if not declared:
            # 没有范围约定。PASS 而不是跳过：一条 PASS 记录能在 §5.1 里
            # 和 FAIL 一起构成分母，跳过则让这个监工看起来从没审过。
            return SupervisorReport(role=self.role, verdict=Verdict.PASS)

        extra = out_of_scope(changed_paths, declared)
        if not extra:
            return SupervisorReport(role=self.role, verdict=Verdict.PASS)

        listed = ", ".join(extra[:_MAX_LISTED])
        if len(extra) > _MAX_LISTED:
            listed += f"（另有 {len(extra) - _MAX_LISTED} 个）"
        return SupervisorReport(
            role=self.role,
            verdict=Verdict.FAIL,
            claims=(
                {
                    "check": "declared-paths-scope",
                    "command": "git diff --name-only HEAD",
                    "expected": f"改动只落在声明范围内：{', '.join(declared)}",
                    "got": f"另外改了 {len(extra)} 个文件：{listed}",
                },
            ),
        )

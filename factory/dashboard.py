"""本地 dashboard：把审计库渲染成一页能给别人看的东西。

这一层**只读**。它不碰 audit.db 的写路径、不改任何裁决、不派发任务 ——
所以它跑挂了、查错了、渲染串行了，最坏后果是这一页不好看。

## 为什么绑 127.0.0.1 且没有认证

审计库里有 attempt 的 prompt、监工 claims、transcript 路径。这些内容够拼出
「这个仓库在做什么、哪里被拦下过」，属于不该对局域网开放的东西。所以：

  bind 固定 127.0.0.1，**不提供 --host 参数**

不给参数是刻意的。给了 `--host 0.0.0.0` 这个口子，它就会出现在某份
README、某个 tmux 快捷键里，然后在咖啡馆的 WiFi 上开着。要给别人看的场景
是「投屏 / 截图 / 同一台机器」，那些都不需要绑 0.0.0.0。真要远程看，走 ssh
端口转发 —— 那条路上有 ssh 的认证，而这里没有任何认证。

`--once` 存在的理由是同一件事的另一半：导出一份静态 HTML 发出去，
比让对方连你机器上的端口更安全，也更方便。

## 为什么不引 flask/fastapi

标准库 http.server 够用（只读、单人看、无并发压力），而项目现在的依赖只有
SQLAlchemy 和 PyYAML。为了一个展示页把 web 框架拖进生产依赖，代价不划算。
"""

from __future__ import annotations

import html
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from factory.audit.models import (
    NOT_DISPATCHED,
    OracleClass,
    Resolution,
    SupervisorRole,
    Verdict,
)
from factory.audit.store import AuditStore
from factory.metrics import gate3_rework, supervisor_metrics

#: dispatcher 里 `_blocked(...)` 的第一个参数全集 —— 也就是「机制闸门」的名字。
#:
#: 这张表**只用于展示分组**：把 claim 分成「机制闸门拦下的」和「监工判的」。
#: 它不参与任何裁决，所以漏一个名字的后果是那条 claim 显示在「其他」组里，
#: 不是漏拦。判决侧一个字都不读这里。
#:
#: 刻意不从 dispatcher 源码里正则抽取：那样做会让展示层依赖另一个模块的
#: 源码写法，dispatcher 改个换行就静默变空。宁可手工维护一张会过时的表，
#: 也不要一张会静默变空的表 —— 前者看得见，后者长得像「这一轮很干净」。
GATE_CLAIMS: dict[str, str] = {
    "diff": "diff 本身为空或读不出来",
    "diff-suppressed": ".gitattributes 把 diff 正文压掉了",
    "fake-green": "金丝雀验出这份绿推不翻",
    "git-config-touched": "worker 动了 .git/config",
    "git-hook-touched": "worker 动了 git hooks",
    "gitlink-added": "新增/改动 mode-160000 的索引条目",
    "harness": "harness 自己报错",
    "head-moved": "HEAD 被移动过",
    "index-skip-flag": "索引跳过标记让改动从 git 眼里消失",
    "info-attributes-touched": ".git/info/attributes 被动过",
    "replace-refs-changed": "refs/replace 让 git 在对象内容上撒谎",
    "runner-hook-added": "新增 runner 自动加载文件（自己出卷子）",
    "shadow-code": "被 .gitignore 挡住的代码文件",
    "spec-criteria-mutated": "worker 改了自己的验收标准",
}


@dataclass
class Row:
    """一次 attempt 在页面上需要的一切。"""

    id: int
    task_id: str
    attempt_no: int
    oracle_class: str
    class_reason: str
    model: str
    harness: str
    resolution: str
    cost_usd: float
    tokens_in: int
    tokens_out: int
    wall_clock_ms: int
    commit: str | None
    created_at: str
    dispatched: bool
    verdicts: tuple[tuple[str, str], ...] = ()
    claims: tuple[dict, ...] = ()

    @property
    def gate_claims(self) -> tuple[dict, ...]:
        return tuple(c for c in self.claims if c.get("check") in GATE_CLAIMS)


@dataclass
class Summary:
    """整库汇总。给别人看的第一屏就是这几个数。"""

    rows: tuple[Row, ...]
    supervisors: dict = field(default_factory=dict)
    rework_mean: float | None = None

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def dispatched(self) -> tuple[Row, ...]:
        """真派发过的那些。

        预分级就拦下的 attempt（D 类硬闸门）从没调过 harness，把它们算进
        「成功率」的分母会让「拦得越多指标越好看」—— 和 metrics.py 里
        gate3_rework 排除 NOT_DISPATCHED 是同一个理由。
        """
        return tuple(r for r in self.rows if r.dispatched)

    @property
    def merged(self) -> int:
        return sum(1 for r in self.rows if r.resolution == Resolution.MERGED)

    @property
    def escalated(self) -> int:
        return sum(1 for r in self.rows if r.resolution == Resolution.ESCALATED)

    @property
    def cost(self) -> float:
        return sum(r.cost_usd for r in self.rows)

    @property
    def merge_rate(self) -> float | None:
        """合并率。分母是**派发过的**，不是全部。

        返回 None 而不是 0.0：一次都没派发过时「0%」是个误导性的数字，
        它长得像「跑了很多次全失败」。None 在页面上渲染成 `—`。
        """
        d = self.dispatched
        return (sum(1 for r in d if r.resolution == Resolution.MERGED) / len(d)
                if d else None)

    @property
    def gate_hits(self) -> Counter:
        """每道机制闸门拦下过几次。页面下半部分就是这张表。"""
        c: Counter = Counter()
        for r in self.rows:
            for cl in r.gate_claims:
                c[cl["check"]] += 1
        return c

    @property
    def class_mix(self) -> Counter:
        return Counter(r.oracle_class for r in self.rows)


def collect(db_path: str | Path, *, task_id: str | None = None) -> Summary:
    """把审计库读成一个 Summary。只读，不写。"""
    store = AuditStore(db_path)
    attempts = (store.attempts_for(task_id) if task_id
                else store.all_attempts())
    rows = []
    for a in attempts:
        rows.append(Row(
            id=a.id, task_id=a.task_id, attempt_no=a.attempt_no,
            oracle_class=str(a.oracle_class), class_reason=a.class_reason,
            model=a.model, harness=a.harness,
            resolution=str(a.resolution), cost_usd=a.cost_usd,
            tokens_in=a.tokens_in, tokens_out=a.tokens_out,
            wall_clock_ms=a.wall_clock_ms, commit=a.commit,
            created_at=a.created_at.strftime("%Y-%m-%d %H:%M"),
            # 标记落在 **harness_version** 上，不是 harness —— dispatcher.py
            # 记预派发拦截时 harness 照样填 adapter 名字（它确实是那个
            # adapter 的任务），只把 version 置成 NOT_DISPATCHED。读错字段的
            # 后果是 D 类进了合并率的分母，而那个方向的错误在页面上看不出来：
            # 数字只会朝好的方向走。判据跟 metrics.py:165 保持同一个。
            dispatched=a.harness_version != NOT_DISPATCHED,
            verdicts=tuple((str(v.role), str(v.verdict)) for v in a.supervisors),
            claims=tuple(c for v in a.supervisors for c in (v.claims or ())),
        ))
    return Summary(
        rows=tuple(rows),
        supervisors=supervisor_metrics(store, task_id=task_id),
        rework_mean=gate3_rework(store).mean_reworks,
    )


# ---------- 渲染 ----------
#
# 手写 HTML 字符串，不引模板引擎：这一页的结构是固定的，而 jinja2 会变成
# 一条生产依赖。每一处插值都过 `_e()`，理由见它自己的注释。

_CSS = """
:root{--bg:#fbfbfa;--fg:#1a1a18;--dim:#6b6b66;--line:#e3e3df;
--ok:#2f7d4f;--bad:#b3341f;--warn:#8a6d1f;--card:#fff}
@media(prefers-color-scheme:dark){:root{--bg:#161614;--fg:#eceae4;
--dim:#9a9a92;--line:#2e2e2a;--ok:#6bbb85;--bad:#e0765c;--warn:#d0ac52;
--card:#1e1e1b}}
*{box-sizing:border-box}
body{margin:0;padding:2rem 1.25rem 4rem;background:var(--bg);color:var(--fg);
font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}
.wrap{max-width:1100px;margin:0 auto}
h1{font-size:1.5rem;margin:0 0 .25rem}
h2{font-size:1.05rem;margin:2.5rem 0 .75rem;padding-bottom:.4rem;
border-bottom:1px solid var(--line)}
.sub{color:var(--dim);margin:0 0 2rem;font-size:.9rem}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
gap:.75rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:.9rem 1rem}
.card .n{font-size:1.6rem;font-weight:600;font-variant-numeric:tabular-nums}
.card .l{color:var(--dim);font-size:.8rem;margin-top:.15rem}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-size:.875rem}
th,td{text-align:left;padding:.5rem .65rem;border-bottom:1px solid var(--line);
white-space:nowrap}
th{color:var(--dim);font-weight:600;font-size:.8rem}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.pill{display:inline-block;padding:.1rem .45rem;border-radius:4px;
font-size:.78rem;font-weight:600}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
.dim{color:var(--dim)}
.wrapline{white-space:normal;max-width:34rem;color:var(--dim);font-size:.82rem}
.empty{color:var(--dim);padding:1.5rem 0;font-style:italic}
.banner{background:color-mix(in srgb,var(--warn) 14%,transparent);
border:1px solid var(--warn);color:var(--warn);border-radius:6px;
padding:.6rem .85rem;margin:0 0 2rem;font-size:.85rem}
code{font:.82rem ui-monospace,SFMono-Regular,Menlo,monospace}
"""


def _e(v) -> str:
    """所有插值的唯一出口。

    审计库里的 class_reason / claims 的 got 字段来自 worker 和 check 的输出 ——
    也就是**模型生成的文本**。它可以包含 `<script>`。这一页只在本机看，但
    `--once` 导出的 HTML 是会发给别人的，所以转义不是可选项。
    """
    return html.escape(str(v), quote=True)


def _cls(resolution: str) -> str:
    return {"merged": "ok", "escalated": "bad", "reworked": "warn"}.get(
        resolution, "dim")


def _pct(v: float | None) -> str:
    """None → `—`。空库时 0% 会被读成「全都失败了」，那是假信息。"""
    return "—" if v is None else f"{v * 100:.0f}%"


def _cards(sm: Summary) -> str:
    cost = f"${sm.cost:.2f}"
    rework = "—" if sm.rework_mean is None else f"{sm.rework_mean:.2f}"
    items = (
        (sm.total, "attempt 总数"),
        (len(sm.dispatched), "真派发过"),
        (_pct(sm.merge_rate), "合并率（分母=派发过）"),
        (sm.escalated, "升级给人"),
        (cost, "累计花费"),
        (rework, "人均打回次数"),
    )
    return '<div class="cards">' + "".join(
        f'<div class="card"><div class="n">{_e(n)}</div>'
        f'<div class="l">{_e(l)}</div></div>' for n, l in items
    ) + "</div>"


def _attempts_table(sm: Summary) -> str:
    if not sm.rows:
        return ('<p class="empty">审计库是空的 —— 跑一次 '
                '<code>factory run</code> 或 <code>factory loop</code> 才有数据。</p>')
    head = ("任务", "轮", "类", "裁决", "监工", "闸门拦下", "模型",
            "花费", "耗时", "commit", "时间")
    rows = []
    for r in sorted(sm.rows, key=lambda x: x.id, reverse=True):
        gates = ", ".join(c["check"] for c in r.gate_claims) or "—"
        # 只列报警的监工，全过就写「4 过」。
        # 逐个列 `role:pass` 会让这一格宽到把 commit/时间挤出视野，而它承载的
        # 信息只有「谁 fail 了」。「4 过」比 `regression:pass, spec:pass, ...`
        # 少一半宽度且没丢东西 —— 但**数字必须打出来**：写死「全过」会让
        # 「四道都跑了且都放行」和「只跑了一道」在页面上长得一样。
        fired = [role for role, v in r.verdicts if v == Verdict.FAIL]
        verd = (", ".join(fired) if fired
                else (f"{len(r.verdicts)} 过" if r.verdicts else "—"))
        secs = f"{r.wall_clock_ms / 1000:.1f}s" if r.wall_clock_ms else "—"
        rows.append(
            f"<tr><td>{_e(r.task_id)}</td>"
            f'<td class="num">{r.attempt_no}</td>'
            f"<td>{_e(r.oracle_class)}</td>"
            f'<td class="pill {_cls(r.resolution)}">{_e(r.resolution)}</td>'
            f'<td class="dim">{_e(verd)}</td>'
            f'<td class="{"bad" if r.gate_claims else "dim"}">{_e(gates)}</td>'
            f'<td class="dim">{_e(r.model)}</td>'
            f'<td class="num">${r.cost_usd:.3f}</td>'
            f'<td class="num">{_e(secs)}</td>'
            f'<td class="dim"><code>{_e((r.commit or "—")[:8])}</code></td>'
            f'<td class="dim">{_e(r.created_at)}</td></tr>'
        )
    return ('<div class="scroll"><table><thead><tr>'
            + "".join(f"<th>{_e(h)}</th>" for h in head)
            + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")


def _supervisor_table(sm: Summary) -> str:
    """监工命中率。直接用 metrics.py 算好的，不在这里重算。

    重算等于把一份判据实现两遍，而两份实现只要有一处不一致，页面上的数字
    和 `factory metrics` 的输出就会对不上 —— 那种矛盾没人能判谁对。
    """
    if not sm.supervisors:
        return '<p class="empty">还没有监工裁决。</p>'
    rows = []
    for role, m in sorted(sm.supervisors.items()):
        hit = _pct(m.hit_rate)
        cph = "—" if m.cost_per_hit is None else f"${m.cost_per_hit:.3f}"
        rows.append(
            f"<tr><td>{_e(role)}</td>"
            f'<td class="dim">{_e(m.verdict_line())}</td>'
            f'<td class="num">{_e(hit)}</td>'
            f'<td class="num">{_e(cph)}</td></tr>'
        )
    return ('<div class="scroll"><table><thead><tr><th>监工</th><th>裁决</th>'
            "<th>命中率</th><th>单次命中成本</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table></div>")


def _gate_table(sm: Summary) -> str:
    """十四道机制闸门，各自拦下过几次。

    「0 次」是这张表里最需要解释的一格，所以它渲染成灰色的 `0` 而不是留空：
    0 的含义是「这道闸门装着、但这个库里没触发过」，而留空会被读成
    「这道闸门不存在」。空表和干净的表长得一样是这个项目里反复出现的坑。
    """
    hits = sm.gate_hits
    rows = []
    for name, desc in sorted(GATE_CLAIMS.items(),
                             key=lambda kv: (-hits.get(kv[0], 0), kv[0])):
        n = hits.get(name, 0)
        rows.append(
            f'<tr><td><code>{_e(name)}</code></td>'
            f'<td class="wrapline">{_e(desc)}</td>'
            f'<td class="num {"bad" if n else "dim"}">{n}</td></tr>'
        )
    return ('<div class="scroll"><table><thead><tr><th>闸门</th><th>它拦什么</th>'
            "<th>拦下次数</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table></div>")


def _claims_detail(sm: Summary) -> str:
    """被闸门拦下的具体现场。给别人看时这一段最有说服力 —— 它是证据。"""
    items = [(r, c) for r in sorted(sm.rows, key=lambda x: x.id, reverse=True)
             for c in r.gate_claims]
    if not items:
        return '<p class="empty">这个库里还没有闸门拦下过任何东西。</p>'
    rows = []
    for r, c in items[:40]:
        rows.append(
            f"<tr><td>{_e(r.task_id)}#{r.attempt_no}</td>"
            f'<td><code class="bad">{_e(c.get("check"))}</code></td>'
            f'<td class="wrapline">{_e(str(c.get("got", ""))[:300])}</td></tr>'
        )
    more = ("" if len(items) <= 40
            else f'<p class="dim">另有 {len(items) - 40} 条未显示。</p>')
    return ('<div class="scroll"><table><thead><tr><th>attempt</th><th>闸门</th>'
            "<th>拦下时看到了什么</th></tr></thead><tbody>"
            + "".join(rows) + f"</tbody></table></div>{more}")


#: 示例库的横幅。判据是**库文件名**而不是一个参数：参数会漏传，而一张编出来的
#: 页面被当成真跑批结果拿出去，是这一页唯一真正有害的失效方式。
_DEMO_BANNER = ('<p class="banner">⚠ 这是 <b>示例数据</b>（<code>factory '
                'dashboard --demo</code> 生成），不是真实跑批结果。</p>')


def render(sm: Summary, *, db: str, title: str = "自动化无人工厂") -> str:
    """整页 HTML。自包含 —— 没有外部 CSS/JS/字体，可以直接发给别人。"""
    banner = _DEMO_BANNER if Path(db).name.startswith("demo") else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)}</title><style>{_CSS}</style></head><body><div class="wrap">
<h1>{_e(title)}</h1>
<p class="sub">调度 + 审计层：任务派给 coding agent，四道监工判收，
十四道机制闸门盯着「这份绿是不是真的」。数据源 <code>{_e(db)}</code>。</p>
{banner}

<h2>跑批结果</h2>
{_cards(sm)}
<h2>每次尝试</h2>
{_attempts_table(sm)}
<h2>监工命中率</h2>
{_supervisor_table(sm)}

<h2>闸门体系</h2>
<p class="sub">这些闸门判的不是「代码好不好」，是「worker 有没有在伪造那份绿」。
每一道都对应一条实测过的攻击路径 —— 拦下次数为 0 不代表它没装上。</p>
{_gate_table(sm)}
<h2>拦下的现场</h2>
{_claims_detail(sm)}
</div></body></html>"""


# ---------- 服务 ----------

#: 固定环回地址。**刻意不做成参数** —— 理由见模块 docstring。
HOST = "127.0.0.1"


def serve(db: str | Path, *, port: int = 8787, task_id: str | None = None) -> int:
    """起一个只读的本地服务。每次请求重新读库，所以刷新就能看到新数据。

    每请求重读而不是启动时缓存：这一页的主要用途之一是「盯着正在跑的批」，
    缓存会让它显示一个不再为真的世界，而那种错误在页面上完全看不出来。
    库是 SQLite、只读、单人看，重读的代价可以忽略。
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib 要求这个名字
            if self.path.startswith("/health"):
                body = b'{"ok":true}'
                ctype = "application/json; charset=utf-8"
            else:
                try:
                    page = render(collect(db, task_id=task_id), db=str(db))
                except Exception as exc:  # noqa: BLE001
                    # 读库失败要**显示出来**，不能渲染成一张空表 ——
                    # 空表和「库里没数据」长得一模一样。
                    page = (f"<!doctype html><meta charset=utf-8>"
                            f"<h1>读 {_e(db)} 失败</h1><pre>{_e(exc)}</pre>")
                body = page.encode("utf-8")
                ctype = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a) -> None:
            """默认会把每个请求打到 stderr，和 loop 的日志混在一起。"""

    with ThreadingHTTPServer((HOST, port), Handler) as srv:
        print(f"dashboard: http://{HOST}:{port}   (Ctrl-C 停)")
        print(f"  数据源 : {db}")
        print("  只绑环回地址，没有认证 —— 要给远程的人看请用 "
              "`factory dashboard --once out.html` 导出后发文件")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\n停了")
    return 0


def export(db: str | Path, out: str | Path, *, task_id: str | None = None) -> int:
    """导出一份自包含的静态 HTML。发给别人比让他们连你的端口安全。"""
    path = Path(out)
    page = render(collect(db, task_id=task_id), db=str(db))
    path.write_text(page, encoding="utf-8")
    print(f"写好了：{path}  ({len(page.encode('utf-8')) / 1024:.0f} KB)")
    print("自包含（无外部 CSS/JS/字体），双击就能看，也能直接发出去。")
    return 0


# ---------- 示例数据 ----------
#
# 真实 audit.db 要跑过批才有东西。做演示时没跑过批就只有一张空页，所以给一条
# `--demo` 路径。它写的库**必须**叫 demo*.db —— render() 靠文件名挂那条
# 「这是示例数据」的横幅，不靠调用方传参数（参数会漏传）。

#: resolution 决定裁决算真阳性还是假阳性（metrics.py:23-25）：REWORKED=真阳性，
#: MERGED/HUMAN_OVERRIDE=假阳性，ESCALATED/PENDING=未定案。示例数据必须**三种都
#: 覆盖**，否则监工那张表整列是「数据不足，继续攒」—— 第一版就是这样，八条里
#: 报警的三条全是 escalated，页面上最该说明问题的一栏反而是空的。
#: defect 是漏报的唯一来源（放行 + 事后挂 defect），所以也得有一条。
_DEMO_ROWS = (
    # (task_id, class, 判据理由, resolution, 花费, 闸门 claim 或 None, defect)
    ("T-101", OracleClass.A, "有可执行判据：pytest tests/test_auth.py",
     Resolution.MERGED, 0.42, None, None),
    ("T-102", OracleClass.A, "有可执行判据：npm test -- --run",
     Resolution.MERGED, 0.31, None, None),
    # 真阳性：闸门报了，人下去看了，确认有问题，打回重做。
    ("T-103", OracleClass.B, "判据要人看一眼渲染结果",
     Resolution.REWORKED, 0.88,
     ("shadow-code", "src/patch.py 被 .gitignore 挡住，但 check 会执行它"), None),
    ("T-104", OracleClass.A, "有可执行判据：pytest -k parser",
     Resolution.REWORKED, 0.55,
     ("gitlink-added", "docs/vendor 8f2a1c9d… (mode 160000)"), None),
    # 未定案：报了警，还没人去看。
    ("T-105", OracleClass.A, "有可执行判据：cargo test",
     Resolution.ESCALATED, 0.37,
     ("runner-hook-added", "新增 tests/conftest.py，让红测试变成退出码 0"), None),
    # 假阳性：报了警，人看完认为没问题，直接放行。
    ("T-106", OracleClass.C, "判据只能靠人跑一遍",
     Resolution.HUMAN_OVERRIDE, 1.20,
     ("diff-suppressed", ".gitattributes 里的 -diff 覆盖了 vendor/"), None),
    ("T-107", OracleClass.A, "有可执行判据：pytest tests/test_api.py",
     Resolution.ESCALATED, 0.29,
     ("fake-green", "金丝雀测试注入后仍然退出 0 —— 这份绿推不翻"), None),
    # 漏报：四道监工全放行，合并了，事后才发现问题。
    ("T-108", OracleClass.A, "有可执行判据：make check",
     Resolution.MERGED, 0.46, None, "D-7：合并后发现分页在空结果上崩"),
)


def build_demo(out: str | Path = "demo.db") -> str:
    """造一份示例审计库，走**真实的 AuditStore 写入路径**。

    不直接拼 SQL：绕过 store 就绕过了脱敏（`redact()` 挂在写边界上），而且
    示例库的形状会和真实库漂移 —— 一张演示页面显示得出来、真实页面显示不出来
    的字段，是比空页更难发现的问题。

    返回库路径。文件名必须以 demo 开头，render() 靠它挂示例横幅。
    """
    path = Path(out)
    if not path.name.startswith("demo"):
        raise ValueError(
            f"示例库必须叫 demo*.db（给的是 {path.name}）—— 横幅靠文件名挂，"
            "改了名字这页就会伪装成真实跑批结果。"
        )
    if path.exists():
        path.unlink()
    store = AuditStore(str(path))

    for task_id, cls, reason, resolution, cost, gate, defect in _DEMO_ROWS:
        aid = store.open_attempt(
            task_id=task_id, spec_ref=[f"§{task_id[-1]}"],
            oracle_class=cls, class_reason=reason,
            harness="claude-code", harness_version="2.1.0",
            model="claude-opus-5",
        )
        store.record_result(
            aid, diff_hash=f"{abs(hash(task_id)):08x}"[:8], commit=None,
            transcript_path=f"/tmp/transcripts/{task_id}.jsonl",
            tokens_in=18_000 + len(task_id) * 900,
            tokens_out=4_200 + len(reason) * 40,
            cost_usd=cost, wall_clock_ms=90_000 + len(reason) * 700,
        )
        # 四道监工都留一条裁决：报表上「某个监工从没被调用过」和「它每次都
        # 放行」是两件事，示例数据不该把它们混在一起。
        for role in (SupervisorRole.REGRESSION, SupervisorRole.SPEC,
                     SupervisorRole.RISK, SupervisorRole.ARCHITECTURE):
            claims: list[dict] = []
            verdict = Verdict.PASS
            if gate and role is SupervisorRole.REGRESSION:
                verdict = Verdict.FAIL
                claims = [{"check": gate[0], "command": "",
                           "expected": "这一轮没动过", "got": gate[1]}]
            store.record_verdict(aid, role=role, verdict=verdict,
                                 claims=claims, tokens=1_800,
                                 cost_usd=0.012)
        if defect:
            # 挂 defect 要在 finalize 前后都成立（它只改 linked_defects），
            # 但放在这里更接近真实时序：人是事后才发现的。
            store.link_defect(aid, defect)
        store.finalize(aid, resolution)

    # 一条 D 类：预分级就拦下，从没派发。它在页面上的作用是证明分母排除了
    # 这一类 —— 少了它，「合并率」那个数字看不出是怎么算的。
    blocked = store.open_attempt(
        task_id="T-109", spec_ref=["§9"], oracle_class=OracleClass.D,
        class_reason="没有任何可执行判据，也说不清人怎么验 —— 退回去补判据",
        harness="claude-code", harness_version=NOT_DISPATCHED,
        model="claude-opus-5",
    )
    store.record_verdict(
        blocked, role=SupervisorRole.RISK, verdict=Verdict.FAIL,
        claims=[{"check": "oracle-class-d", "command": "",
                 "expected": "A/B/C", "got": "D：无判据，不派发"}],
    )
    store.finalize(blocked, Resolution.ESCALATED)
    return str(path)

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
import re
import secrets
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import yaml

from factory.audit.models import (
    NOT_DISPATCHED,
    OracleClass,
    Resolution,
    SupervisorRole,
    Verdict,
)
from factory.audit.store import AuditStore
from factory.backlog.store import (
    BLOCKED,
    DONE,
    INBOX,
    NEEDS_HUMAN,
    RUNNING,
    STATES,
    Backlog,
)
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
    "head-moved": "HEAD 被移动过",
    "index-skip-flag": "索引跳过标记让改动从 git 眼里消失",
    "info-attributes-touched": ".git/info/attributes 被动过",
    "replace-refs-changed": "refs/replace 让 git 在对象内容上撒谎",
    "runner-hook-added": "新增 runner 自动加载文件（自己出卷子）",
    "shadow-code": "被 .gitignore 挡住的代码文件",
    "spec-criteria-mutated": "worker 改了自己的验收标准",
}

#: 不是闸门 —— 是我们这一侧或上游坏了。
#:
#: `harness` 曾经在 GATE_CLAIMS 里，一次真跑批把这件事暴露出来：网关回了个
#: 502，attempt 记成 `reworked` + 红色「闸门 harness」。于是一条**处理得完全
#: 正确**的链路（打回 → 重试 → 合并）在页面上长成「模型写错了、被闸门拦下」。
#: 客户读到的是两件都不成立的事：模型没写错，闸门也没拦任何东西。
#: 同一个形状在 P0 判据上踩过一次 —— 上游抖动不许把正确的链路判红。
#:
#: 分开的第二个理由是那张命中率表：harness 混在里面会让「闸门拦下 N 次」
#: 里掺进一堆 502。拿它做汇报的数就系统性虚高，而虚高的方向是**对我们有利**
#: 的那一侧 —— 这种偏差没人会来纠。
#:
#: 判据仍是 claim 的 check 名，和 GATE_CLAIMS 同一套读法；两张表**不许合并**，
#: 因为合并之后「闸门拦下」和「工具坏了」就只剩一个计数器。
FAULT_CLAIMS: dict[str, str] = {
    "harness": "worker CLI 自己报错（上游 5xx、超时、装的东西不对）",
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

    @property
    def fault_claims(self) -> tuple[dict, ...]:
        """工具/上游故障。和 gate_claims 是**互斥**的两组，见 FAULT_CLAIMS。"""
        return tuple(c for c in self.claims if c.get("check") in FAULT_CLAIMS)


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

        被 `FAULT_CLAIMS`（502、超时）打回的 attempt **留在分母里**，这是刻意的，
        和 `metrics.gate3_rework` 把它们排掉恰好相反 —— 两个数问的不是同一件事：
        gate3 问「验收条件够不够机器可判定」，502 对那件事一个字都没说；
        这个数问「端到端要几次才出货」，而 502 是那个「几次」的真实组成部分。

        排掉它的代价是致命的：网关九成时间挂着的日子会显示 100% 合并率，和上游
        完美的日子长得一模一样，而偏差方向又是「系统很健康」，没人会来纠。留在
        分母里则天然可见，横条上紧挨着的「工具故障」计数说明那一半是谁的锅。
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
    def fault_hits(self) -> Counter:
        """工具/上游故障各出过几次。**不进** gate_hits，见 FAULT_CLAIMS。"""
        c: Counter = Counter()
        for r in self.rows:
            for cl in r.fault_claims:
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


# ---------- 队列侧（树的数据源） ----------
#
# 依赖边只存在于**队列目录里的 YAML**（`depends_on`），审计库里没有这个字段。
# 所以树要读第二个数据源。仍然只读：这一层一个字节都不往队列里写。

#: 队列撞名后缀（`T-x.2.yaml`）。和 backlog/store.py 里的 `_PARK_SUFFIX` 同一件
#: 事，刻意不 import 它 —— 那是私有名字，import 私有名会在对面改名时静默失配。
#: 这里失配的后果只是树上多一个孤立节点（看得见），不是判决出错。
_QSUFFIX = re.compile(r"\.\d+$")


@dataclass(frozen=True)
class QueueEntry:
    """队列里的一个条目。`name` 是**文件名 stem**，也就是队列层的身份。"""

    name: str
    state: str
    depends_on: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    title: str = ""

    @property
    def ident(self) -> str:
        """剥掉撞名后缀的身份 —— 依赖边和 attempt 都按这个对上。"""
        return _QSUFFIX.sub("", self.name)


@dataclass
class QueueState:
    """队列的当下形状。给树、给 `/state.json`、给「谁在等谁」共用一份。"""

    entries: tuple[QueueEntry, ...] = ()
    counts: dict[str, int] = field(default_factory=dict)
    running: tuple[str, ...] = ()
    waiting: tuple[tuple[str, tuple[str, ...]], ...] = ()
    root: str | None = None
    #: 读队列失败的原因。**必须渲染出来** —— 读不到队列和队列是空的，
    #: 在页面上长得一模一样，而前者意味着这一页在说谎。
    error: str = ""

    @property
    def by_ident(self) -> dict[str, QueueEntry]:
        return {e.ident: e for e in self.entries}


def queue_state(root: str | Path | None) -> QueueState:
    """读队列目录。给 None（没传 `--queue`）就返回空壳，不报错。

    任何异常都收进 `.error` 而不是往上抛：这一页挂掉的代价是客户面前一片白，
    而队列读不出来时页面其余部分（审计库那半边）仍然是有效信息。
    """
    if root is None:
        return QueueState()
    try:
        bl = Backlog(root)
        # 先判「这地方到底是不是一个队列」。不判的话下面每个 `exists()` 都返
        # False，于是路径写错、目录被删、指到一个文件上，全都渲染成「队列是空
        # 的」—— 一个空队列在页面上是安静的，而这三种情况都需要人动手。
        # 判据取「STATES 里有没有任何一个子目录存在」而不是「根目录在不在」：
        # 一个刚 ensure() 过的空队列几个子目录都在、条目为零，那是真的空。
        if not Path(root).is_dir():
            return QueueState(root=str(root),
                              error=f"{root} 不是一个目录")
        if not any(bl.dir(s).is_dir() for s in STATES):
            return QueueState(
                root=str(root),
                error=f"{root} 下面没有 inbox/running/done… 这些目录，"
                      "不像一个队列（路径写错了？还是没跑过 factory prd --queue？）")
        entries: list[QueueEntry] = []
        merged = bl.merged_ids() if bl.dir(DONE).exists() else frozenset()
        for state in STATES:
            d = bl.dir(state)
            if not d.exists():
                continue
            for p in bl._entries(d):
                deps = _read_deps(p)
                entries.append(QueueEntry(
                    name=p.stem, state=state, depends_on=deps,
                    missing=tuple(x for x in deps if x not in merged),
                    title=_read_title(p),
                ))
        return QueueState(
            entries=tuple(entries),
            counts=bl.counts(),
            running=tuple(p.stem for p in bl.running()),
            waiting=tuple((p.stem, miss) for p, miss in bl.blocked_by_deps()),
            root=str(root),
        )
    except Exception as exc:  # noqa: BLE001
        return QueueState(root=str(root), error=f"{type(exc).__name__}: {exc}")


def _read_deps(path: Path) -> tuple[str, ...]:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return ()
    if not isinstance(doc, dict):
        return ()
    return tuple(str(d) for d in (doc.get("depends_on") or ()))


def _read_title(path: Path) -> str:
    """节点上显示的一句话。读不出来返回空字符串，节点照样画出来。"""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return "（YAML 读不出来）"
    if not isinstance(doc, dict):
        return "（YAML 不是一个映射）"
    text = str(doc.get("prompt") or doc.get("acceptance") or "")
    return text.strip().splitlines()[0][:90] if text.strip() else ""


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
.tree details{margin:0}
.tree>details,.tree>.node{margin-bottom:.4rem}
details.node{border-left:2px solid var(--line);padding-left:.7rem}
details.node>summary{cursor:pointer;padding:.35rem 0;display:flex;
flex-wrap:wrap;gap:.5rem;align-items:baseline}
details.node>summary::marker{color:var(--dim)}
details.node details.node{margin-top:.2rem}
.node .ttl{font-size:.82rem;flex:1 1 14rem;min-width:0;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}
.node .wait{color:var(--warn);font-size:.8rem}
.node.dup{padding:.35rem 0 .35rem .7rem;font-size:.85rem}
.round{display:flex;flex-wrap:wrap;gap:.6rem;align-items:baseline;
padding:.3rem 0 .3rem .2rem;font-size:.83rem;border-bottom:1px dashed var(--line)}
.round .rno{font-weight:600;min-width:4.5rem}
.pill.run{color:var(--warn)}
.cards.assume{margin-top:.6rem}
.cards.assume .card{border-style:dashed;border-color:var(--warn)}
.assume-note{margin:.5rem 0 0;font-size:.82rem}
.live{display:flex;flex-wrap:wrap;gap:.5rem 1.2rem;font-size:.85rem;
color:var(--dim);align-items:baseline}
.live b{color:var(--fg);font-variant-numeric:tabular-nums}
.dot{display:inline-block;width:.5rem;height:.5rem;border-radius:50%;
background:var(--dim);margin-right:.35rem}
.dot.on{background:var(--ok);animation:p 1.4s ease-in-out infinite}
@keyframes p{0%,100%{opacity:1}50%{opacity:.25}}
form.act{display:flex;flex-wrap:wrap;gap:.5rem;align-items:flex-start;
margin:0 0 1rem}
form.act textarea,form.act input[type=text],form.act input[type=number]{
font:inherit;padding:.45rem .55rem;border:1px solid var(--line);
border-radius:6px;background:var(--card);color:var(--fg)}
form.act textarea{flex:1 1 22rem;min-height:4.5rem}
form.act button{font:inherit;font-weight:600;padding:.45rem 1rem;
border:1px solid var(--line);border-radius:6px;background:var(--card);
color:var(--fg);cursor:pointer}
.nav{position:sticky;top:0;z-index:5;display:flex;gap:.15rem;
overflow-x:auto;margin:1.25rem 0 0;padding:.35rem 0;background:var(--bg);
border-bottom:1px solid var(--line);scrollbar-width:none}
.nav::-webkit-scrollbar{display:none}
.nav a{color:var(--dim);text-decoration:none;font-size:.88rem;font-weight:600;
padding:.4rem .7rem;border-radius:6px;white-space:nowrap}
.nav a:hover{color:var(--fg);background:var(--card)}
.nav a[aria-current]{color:var(--fg);background:var(--card);
box-shadow:inset 0 0 0 1px var(--line)}
.nav a.alt{margin-left:auto;font-weight:400}
.view{display:none;scroll-margin-top:3.25rem}
.view.on,.view:target{display:block}
.view>h2:first-child{margin-top:1.25rem}
html.showall .view{display:block}
@media print{.view{display:block}}
"""

#: 3 秒轮询 `/state.json`，只替换标了 `data-live` 的那几个数字。
#:
#: 为什么不是 `<meta http-equiv=refresh>`：整页刷会丢滚动位置和所有 `details`
#: 的展开态 —— 而这一页的主体就是一棵手动展开的树，客户演示时刷一下全合上。
#: 为什么不是 SSE：`ThreadingHTTPServer` 上一个挂住的长连接占掉一个线程，
#: 演示场合宁可多几个短请求。
#: 为什么整个函数包在 try 里：这段脚本报错也不许把页面弄坏 —— 静态导出
#: （`--once`）里 `/state.json` 根本不存在，那时它必须安静地失败。
_LIVE_JS = """
(function(){
  var tick=function(){
    fetch('/state.json',{cache:'no-store'}).then(function(r){
      if(!r.ok) throw 0; return r.json();
    }).then(function(s){
      document.querySelectorAll('[data-live]').forEach(function(el){
        var v=s[el.getAttribute('data-live')];
        if(v!==undefined&&v!==null) el.textContent=String(v);
      });
      var d=document.querySelector('.dot');
      if(d) d.className='dot'+(s.running_n>0?' on':'');
    }).catch(function(){});
  };
  tick(); setInterval(tick,3000);
})();
"""

#: 分栏切换。纯前端，**不是路由** —— 服务端仍然一次把所有栏渲染进同一份 HTML，
#: 所以 `--once` 导出的单文件里分栏照样能用（这是不做服务端路由的唯一理由）。
#:
#: 三层保险，因为「一张空白页」和「这一栏没数据」在浏览器里长得一样：
#: 1. 默认那一栏的 `class="view on"` 是**服务端渲染**的 —— JS 挂了也有内容；
#: 2. CSS 里有 `.view:target`，所以就算这段脚本整个不执行，点导航仍然切得动；
#: 3. hash 指到一个不存在的栏时回落到第一栏，不是谁都不显示。
#: `#all` 走 `html.showall`（给 Ctrl-F 和打印用），刻意不复制一份内容出来：
#: 复制会让同一个 id 在页面上出现两次，而重复 id 会让上面的 `data-live` 只更新
#: 第一个 —— 那种错误在页面上完全看不出来。
_NAV_JS = """
(function(){
  var nav=document.querySelector('.nav');
  if(!nav) return;
  var views=[].slice.call(document.querySelectorAll('.view'));
  if(!views.length) return;
  var apply=function(){
    var h=(location.hash||'').replace('#',''), all=(h==='all');
    document.documentElement.classList.toggle('showall',all);
    var hit=null;
    if(!all){
      views.forEach(function(v){ if(v.id===h) hit=v; });
      if(!hit) hit=views[0];
      views.forEach(function(v){ v.classList.toggle('on',v===hit); });
    }
    var cur=all?'all':hit.id;
    nav.querySelectorAll('a').forEach(function(a){
      var t=(a.getAttribute('href')||'').replace('#','');
      if(t===cur) a.setAttribute('aria-current','page');
      else a.removeAttribute('aria-current');
    });
  };
  addEventListener('hashchange',function(){ apply(); scrollTo(0,0); });
  apply();
  // 带 hash 进来时 .nav 是 sticky 的，会正好压住这一栏的 <h2>。每一栏都当成
  // 「一页」看，所以首屏一律回到顶部。
  //
  // **必须延到 load 之后**。浏览器的「滚到锚点」发生在这段脚本执行完之后，
  // 写成同步的 scrollTo(0,0) 会被它反过来覆盖：实测 scrollY 仍是 313.5，
  // <h2> 正好卡在导航底下（h2.top=0 < nav.bottom=48）。
  // 而这种错在静态 HTML 上断言不出来 —— 那行 scrollTo 确实在文件里，
  // 差的只是它跑在哪一刻。只有在真浏览器里量几何才看得见。
  // 没有 JS 时这件事由 .view 上的 scroll-margin-top 兜着。
  if(location.hash){
    var top=function(){ scrollTo(0,0); };
    addEventListener('load',top);
    requestAnimationFrame(top);
  }
})();
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
    head = ("任务", "轮", "类", "裁决", "监工", "闸门拦下", "工具故障", "模型",
            "花费", "耗时", "commit", "时间")
    rows = []
    for r in sorted(sm.rows, key=lambda x: x.id, reverse=True):
        gates = ", ".join(c["check"] for c in r.gate_claims) or "—"
        # 故障单列一格。塞进「闸门拦下」那一格会让汇总的红色数量虚高，
        # 而虚高的方向恰好对我们有利 —— 那种偏差没人会来纠。
        faults = ", ".join(c["check"] for c in r.fault_claims) or "—"
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
            f'<td class="{"warn" if r.fault_claims else "dim"}">{_e(faults)}</td>'
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
    """每道机制闸门各自拦下过几次。

    刻意不在这里写道数：这段 docstring 曾经写着「十四道」，而 `harness` 挪进
    FAULT_CLAIMS 之后表变成 13 行、这行字没跟着改。一个会过时的数字写在
    注释里没有任何测试盯得住 —— 那道数唯一的来源是 `len(GATE_CLAIMS)`。

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
    # 故障那张表跟在后面，同样的「0 渲染成灰 0」规则。分成两张而不是加一列
    # 「类型」：一列类型仍然会被求和成一个「拦下 N 次」，而这两个数不该相加。
    fh = sm.fault_hits
    frows = [
        f'<tr><td><code>{_e(name)}</code></td>'
        f'<td class="wrapline">{_e(desc)}</td>'
        f'<td class="num {"warn" if fh.get(name, 0) else "dim"}">'
        f'{fh.get(name, 0)}</td></tr>'
        for name, desc in sorted(FAULT_CLAIMS.items())
    ]
    return ('<div class="scroll"><table><thead><tr><th>闸门</th><th>它拦什么</th>'
            "<th>拦下次数</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table></div>"
            '<p class="sub">下面这些<b>不是闸门</b> —— 是我们这一侧或上游坏了。'
            "分开数是因为混在一起会让「闸门拦下 N 次」掺进一堆 502，"
            "而那个数是要拿出去讲的。</p>"
            '<div class="scroll"><table><thead><tr><th>故障</th><th>是什么</th>'
            "<th>出现次数</th></tr></thead><tbody>"
            + "".join(frows) + "</tbody></table></div>")


def _claims_detail(sm: Summary) -> str:
    """打回/拦下的具体现场。给别人看时这一段最有说服力 —— 它是证据。

    收**所有** claim，不只是机制闸门那几个。原本只收 `gate_claims`，于是三种
    reason 在这张表上根本不存在：`no-checks-defined`（任务没有可执行判据，
    回归监工据此拒绝判过）、`declared-paths-scope`（改到了声明之外的文件）、
    `post-diff-grading`（拿真实 diff 重分级后升级）。它们都是**真的打回理由**，
    而页面此前只在轮次那行显示「监工 regression」—— 说了谁反对，没说为什么。
    演示时「为什么被打回」是第一个被问到的问题。

    每条标出属于哪一类（闸门 / 工具故障 / 监工），因为处置完全不同：闸门是
    worker 越界、故障是重试、监工是去看那条 claim 的正文。
    """
    kinds = {}
    for name in GATE_CLAIMS:
        kinds[name] = ("闸门", "bad")
    for name in FAULT_CLAIMS:
        kinds[name] = ("工具故障", "warn")
    items = [(r, c) for r in sorted(sm.rows, key=lambda x: x.id, reverse=True)
             for c in r.claims]
    if not items:
        return '<p class="empty">这个库里还没有任何 claim —— 没人反对过。</p>'
    rows = []
    for r, c in items[:40]:
        # 表里没有的名字算「监工」而不是丢掉：一张手维护的表迟早漏名字，
        # 而漏掉的后果必须是「归错类」，不能是「这条证据不存在」。
        kind, cls = kinds.get(str(c.get("check")), ("监工", "dim"))
        rows.append(
            f"<tr><td>{_e(r.task_id)}#{r.attempt_no}</td>"
            f'<td class="{cls}">{_e(kind)}</td>'
            f'<td><code class="{cls}">{_e(c.get("check"))}</code></td>'
            f'<td class="wrapline">{_e(str(c.get("got", ""))[:300])}</td></tr>'
        )
    more = ("" if len(items) <= 40
            else f'<p class="dim">另有 {len(items) - 40} 条未显示。</p>')
    return ('<div class="scroll"><table><thead><tr><th>attempt</th><th>类</th>'
            "<th>什么</th><th>当时看到了什么</th></tr></thead><tbody>"
            + "".join(rows) + f"</tbody></table></div>{more}")


_STATE_CLS = {"done": "ok", "needs-human": "warn", "blocked": "bad",
              "running": "run", "inbox": "dim"}


def _tree(sm: Summary, qs: QueueState) -> str:
    """任务树：依赖边来自队列 YAML，每个节点下挂它的每一轮 attempt。

    没有依赖时**退化成平表**（每个任务一个顶层节点），不是显示成空 ——
    一个刚起步、还没人写 depends_on 的仓库，树该是一排根，不是一片白。

    队列读不出来时也不留白：审计库里有 task_id，照样把节点画出来，只是没有边。
    """
    if qs.error:
        head = ('<p class="banner">读队列 <code>' + _e(qs.root)
                + "</code> 失败，下面这棵树没有依赖边（只按审计库里的任务列）："
                + f"<code>{_e(qs.error)}</code></p>")
    elif qs.root is None:
        head = ('<p class="sub">没给 <code>--queue</code>，所以没有依赖边 —— '
                "下面按审计库里出现过的任务平铺。</p>")
    else:
        head = ""

    by_task: dict[str, list[Row]] = {}
    for r in sm.rows:
        by_task.setdefault(r.task_id, []).append(r)

    ents = {e.ident: e for e in qs.entries}
    # 节点全集 = 队列里的 ∪ 审计库里跑过的。只取其一都会漏：队列条目合并后
    # 仍在 done/（有），而 `--queue` 没传时队列侧一个都没有。
    names = sorted(set(ents) | set(by_task))
    children: dict[str, list[str]] = {n: [] for n in names}
    roots = []
    for n in names:
        parents = [d for d in (ents[n].depends_on if n in ents else ())
                   if d in children]
        if parents:
            for p in parents:
                children[p].append(n)
        else:
            roots.append(n)
    if not names:
        return (head + '<p class="empty">队列和审计库都还没有任务。</p>')

    seen: set[str] = set()
    body = "".join(_node(n, children, ents, by_task, seen) for n in roots)
    # 环会让上面的遍历漏掉节点。漏掉不许悄悄漏 —— 补在后面并说明原因，
    # 否则「树上少了三个任务」和「这三个任务不存在」在页面上一样。
    left = [n for n in names if n not in seen]
    if left:
        body += ('<p class="banner">下面这些没能挂进树（依赖成环，或前置只存在于'
                 "另一个队列）：</p>"
                 + "".join(_node(n, {**children, n: []}, ents, by_task, seen)
                           for n in left))
    return head + f'<div class="tree">{body}</div>'


def _node(name: str, children: dict[str, list[str]], ents: dict[str, QueueEntry],
          by_task: dict[str, list[Row]], seen: set[str]) -> str:
    if name in seen:  # 菱形依赖：同一个节点两个父亲。只画第一次。
        return (f'<div class="node dup dim"><code>{_e(name)}</code> '
                "<span>（已在上面展开过）</span></div>")
    seen.add(name)
    e = ents.get(name)
    state = e.state if e else "—"
    cls = _STATE_CLS.get(state, "dim")
    rows = sorted(by_task.get(name, []), key=lambda r: r.attempt_no)
    # 「等 X」只挂在 inbox 条目上。一个已经合并（done/）的条目，它的前置当时
    # 满足没满足是历史，不是现在在等 —— 挂上去会让人以为队列卡住了。
    # 判据取 state 而不是 `e.missing` 本身：missing 对每个状态都算得出来，
    # 而只有 inbox 里的「算得出来」等于「现在动不了」。
    waiting = (f'<span class="wait">等 {_e(", ".join(e.missing))}</span>'
               if e and e.missing and e.state == INBOX else "")
    # attempt 条数**总是打出来**（0 也打）：留白会被读成「这个任务没跑过」，
    # 而它也可能是「跑过但审计库是另一个」。
    meta = (f'<span class="pill {cls}">{_e(state)}</span>'
            f'<span class="dim">{len(rows)} 轮</span>{waiting}')
    title = f'<span class="ttl dim">{_e(e.title)}</span>' if e and e.title else ""
    inner = _rounds(rows) + "".join(
        _node(c, children, ents, by_task, seen) for c in sorted(children[name]))
    return (f'<details class="node" open><summary><code>{_e(name)}</code>'
            f"{meta}{title}</summary>{inner}</details>")


def _rounds(rows: tuple[Row, ...] | list[Row]) -> str:
    """一个任务的每一轮。升序 —— 编码→测试→打回→再编码是**从上往下**读的。

    倒序在页面上完全说得通（最新的在上面），所以这里必须有测试钉住方向：
    两种顺序都渲染出一张好看的表，只有一种讲对了故事。
    """
    if not rows:
        return '<p class="empty">这个任务在审计库里还没有 attempt。</p>'
    out = []
    for r in rows:
        gates = ", ".join(c["check"] for c in r.gate_claims)
        fired = [role for role, v in r.verdicts if v == Verdict.FAIL]
        # 顺序是判断：**故障先说**。一轮里既有故障又有监工报警时，说「工具坏了」
        # 比说「监工不同意」更接近人要做的下一件事（去看网关，而不是去看代码）。
        if gates:
            why = f'<span class="bad">闸门 {_e(gates)}</span>'
        elif r.fault_claims:
            why = ('<span class="warn">工具/上游故障 '
                   f'{_e(", ".join(c["check"] for c in r.fault_claims))}'
                   " —— 不是代码问题，重试即可</span>")
        elif fired:
            why = f'<span class="warn">监工 {_e(", ".join(fired))}</span>'
        else:
            why = '<span class="dim">无人反对</span>'
        out.append(
            f'<div class="round"><span class="rno">第 {r.attempt_no} 轮</span>'
            f'<span class="pill {_cls(r.resolution)}">{_e(r.resolution)}</span>'
            f"{why}"
            f'<span class="dim">${r.cost_usd:.3f} · '
            f"{r.wall_clock_ms / 1000:.0f}s · {_e(r.model)}</span></div>")
    return "".join(out)


#: 「一个人手做这件事要多久」的默认假设，小时。
#:
#: 这是**编的数字**，不是测出来的。所以它在页面上必须带「假设」字样并且可改 ——
#: 把一个我们拍的数字显示成实测值，是这一页唯一能造成真实损害的失效方式
#: （客户会拿它做决定）。同一个理由：默认值取偏保守的一端。
MANUAL_HOURS = 2.0


def _ledger(sm: Summary, *, manual_hours: float = MANUAL_HOURS) -> str:
    """成本与人力账。左边是实测，右边是假设，两边**在视觉上分开**。"""
    tasks = len({r.task_id for r in sm.rows})
    wall_h = sum(r.wall_clock_ms for r in sm.rows) / 3_600_000
    saved = manual_hours * tasks - wall_h
    real = (
        (f"${sm.cost:.2f}", "实测 · 模型花费"),
        (f"{wall_h:.2f} h", "实测 · 机器墙钟"),
        (sm.total, "实测 · attempt 条数"),
        (tasks, "实测 · 任务数"),
    )
    return (
        '<div class="cards">' + "".join(
            f'<div class="card"><div class="n">{_e(n)}</div>'
            f'<div class="l">{_e(l)}</div></div>' for n, l in real)
        + "</div>"
        + '<div class="cards assume"><div class="card">'
        f'<div class="n">{manual_hours:g} h</div>'
        '<div class="l">假设 · 人工单任务估时（可改）</div></div>'
        f'<div class="card"><div class="n">{saved:.1f} h</div>'
        '<div class="l">假设 · 省下的人力 = 估时×任务数 − 墙钟</div></div>'
        "</div>"
        '<p class="sub assume-note">⚠ 上面第二排两个数字里含<b>人工填的假设</b>'
        "（单任务估时），不是测量结果。改这个假设：<code>?manual_hours=4</code>。"
        "第一排四个数字全部来自审计库。</p>")


#: 示例库的横幅。判据是**库文件名**而不是一个参数：参数会漏传，而一张编出来的
#: 页面被当成真跑批结果拿出去，是这一页唯一真正有害的失效方式。
_DEMO_BANNER = ('<p class="banner">⚠ 这是 <b>示例数据</b>（<code>factory '
                'dashboard --demo</code> 生成），不是真实跑批结果。</p>')


def state_payload(sm: Summary, qs: QueueState) -> dict:
    """`/state.json` 的内容。**只有聚合出来的数字和 task_id。**

    刻意不放 prompt / class_reason / claims 的 got / transcript 路径：那些是
    模型和 worker 的原文，可能含仓库内容甚至凭据。整页 HTML 里有它们是因为
    那一页要人主动打开看；一个 3 秒被拉一次的 JSON 端点不该背同样的东西。
    """
    return {
        "queue": dict(qs.counts),
        # 各目录条数再拍平一份 `q_inbox` 这样的键：轮询脚本按 `data-live` 的
        # 名字直接取值，不许让它在 JSON 里钻嵌套 —— 钻错一层就是静默不更新。
        **{f"q_{k}": v for k, v in qs.counts.items()},
        "running": list(qs.running),
        "running_n": len(qs.running),
        "waiting": [{"task": n, "missing": list(m)} for n, m in qs.waiting],
        "waiting_n": len(qs.waiting),
        "attempts": sm.total,
        "merged": sm.merged,
        "escalated": sm.escalated,
        "cost": round(sm.cost, 4),
        "gate_hits_n": sum(sm.gate_hits.values()),
        # 单独一个键。掺进 gate_hits_n 会让「闸门拦下」在轮询里也虚高，
        # 而这是唯一一个会被人截图去汇报的数。
        "fault_hits_n": sum(sm.fault_hits.values()),
        "queue_error": qs.error,
    }


def _live_bar(sm: Summary, qs: QueueState) -> str:
    """页面顶部那条会动的横条。静态导出里它就是一个定格的快照。

    每个数字都有 `data-live` 名字，且**初值直接渲染在 HTML 里** —— 不靠 JS
    填。JS 挂了（或者这是一份离线导出）的话，看到的是一个正确的旧数字，
    不是一排空格。
    """
    s = state_payload(sm, qs)
    q = s["queue"]
    cells = (
        ("inbox 待跑", "q_inbox", q.get("inbox", 0)),
        ("正在跑", "running_n", s["running_n"]),
        ("等前置", "waiting_n", s["waiting_n"]),
        ("已合并", "merged", s["merged"]),
        ("升级给人", "escalated", s["escalated"]),
        ("闸门拦下", "gate_hits_n", s["gate_hits_n"]),
        # 故障也上横条。不上的话，一次上游宕机在这条横条上和一切正常长得一样
        # （闸门 0、合并数不动），而人正是靠这条横条判断「要不要去看一眼」。
        ("工具故障", "fault_hits_n", s["fault_hits_n"]),
        ("累计花费 $", "cost", f'{s["cost"]:.2f}'),
    )
    dot = f'<span class="dot{" on" if s["running_n"] else ""}"></span>'
    return ('<p class="live">' + dot + "".join(
        f'<span>{_e(label)} <b data-live="{key}">{_e(val)}</b></span>'
        for label, key, val in cells)
        + '<span class="dim">每 3s 自动刷新（只换数字，树不会合上）</span></p>')


def _actions(token: str, *, launchd: str) -> str:
    """三个表单。它们只是 CLI 的壳 —— 提交后调的就是 `factory prd` 那条路。

    没有「开始跑批」按钮：`loop` 带预算上限、漏账熔断、超时杀孤儿，
    一个网页按钮绕过其中任何一条都是静默的（页面上只会显示「已开始」）。
    """
    t = f'<input type="hidden" name="token" value="{_e(token)}">'
    return f"""
<form class="act" method="post" action="/prd">{t}
<textarea name="text" required placeholder="口语化说清要做什么。会走完整闸门：
没有可执行判据、碰了危险 op、spec_ref 缺失，都会被拦下并把原因显示出来。"
></textarea>
<button type="submit">提需求 → 过闸门 → 入队</button></form>
<form class="act" method="post" action="/override">{t}
<input type="text" name="attempt_id" required placeholder="attempt id" size="8">
<input type="text" name="resolution" required placeholder="merged / escalated"
size="14">
<button type="submit">人工定案</button></form>
<form class="act" method="post" action="/defect">{t}
<input type="text" name="attempt_id" placeholder="attempt id（可空）" size="14">
<input type="text" name="commit" placeholder="或 commit sha" size="12">
<input type="text" name="defect_id" required placeholder="defect id" size="12">
<button type="submit">报缺陷（记漏报）</button></form>
<p class="sub">跑批<b>不在这个页面上</b>：用 <code>factory loop</code>，
或让 launchd 定时拉起（预算上限、漏账熔断、超时杀孤儿都在那条路上）。
当前 launchd：{_e(launchd)}</p>"""


def _views(sm: Summary, qs: QueueState, *, manual_hours: float,
           token: str, launchd: str) -> list[tuple[str, str, str]]:
    """分栏的**唯一来源**：(slug, 导航标签, 这一栏的 HTML)。

    导航和栏体都从这一个列表生成，所以两边不可能对不上。分两处写的话，一个
    「导航上有、栏体没有」的条目点下去是一张**空白页** —— 而空白页和「这一栏
    本来就没数据」长得一模一样，那正是这个项目里反复吃亏的形状。

    「人工介入」那一栏是**条件加进来**的，不是渲染成一个空栏：静态导出里没有
    token，一个点不动的表单栏比没有这一栏更糟（见 `_actions`）。
    """
    vs = [
        ("overview", "总览", f"""<h2>跑批结果</h2>
{_cards(sm)}
<h2>成本与人力账</h2>
{_ledger(sm, manual_hours=manual_hours)}"""),
        ("flow", "任务流转", f"""<h2>任务树 · 每一轮往复</h2>
<p class="sub">树边是真依赖（前置没合并就不认领）。展开一个任务能看到它的
每一轮：编码 → 测试 → 被谁打回 → 再编码。</p>
{_tree(sm, qs)}"""),
        ("verdicts", "监工判收", f"""<h2>监工命中率</h2>
<p class="sub">四道监工各自判了多少、其中多少是真拦对了。命中率低不等于该关掉它 ——
先看右边那列单次命中成本。</p>
{_supervisor_table(sm)}
<h2>打回的现场：谁反对、以及看到了什么</h2>
{_claims_detail(sm)}"""),
        ("gates", "防伪闸门", f"""<h2>闸门体系</h2>
<p class="sub">这些闸门判的不是「代码好不好」，是「worker 有没有在伪造那份绿」。
每一道都对应一条实测过的攻击路径 —— 拦下次数为 0 不代表它没装上。</p>
{_gate_table(sm)}"""),
        ("attempts", "每次尝试", f"""<h2>每次尝试</h2>
<p class="sub">一行一个 attempt，最近的在上面。这是审计库的原始形状，
上面几栏的数字都从这里聚出来。</p>
{_attempts_table(sm)}"""),
    ]
    if token:
        vs.append(("actions", "提需求 · 人工介入",
                   f"<h2>提需求 / 人工介入</h2>{_actions(token, launchd=launchd)}"))
    return vs


def _nav(views: list[tuple[str, str, str]]) -> str:
    """顶部导航。第一栏带 `aria-current` 是**服务端**给的，和 `.view.on` 同一个
    下标 —— JS 没跑起来时导航的高亮和实际显示的那一栏仍然是一致的。
    """
    links = []
    for i, (slug, label, _body) in enumerate(views):
        cur = ' aria-current="page"' if i == 0 else ""
        links.append(f'<a href="#{_e(slug)}"{cur}>{_e(label)}</a>')
    return ('<nav class="nav">' + "".join(links)
            + '<a class="alt" href="#all">全部展开</a></nav>')


def render(sm: Summary, *, db: str, title: str = "自动化无人工厂",
           qs: QueueState | None = None, manual_hours: float = MANUAL_HOURS,
           token: str = "", launchd: str = "未检测") -> str:
    """整页 HTML。自包含 —— 没有外部 CSS/JS/字体，可以直接发给别人。

    `token` 为空（静态导出、`--once`）时**不渲染表单**：一份发出去的 HTML 里
    带着能 POST 的表单没有意义，而它会让看的人以为按了有用。
    """
    qs = qs if qs is not None else QueueState()
    banner = _DEMO_BANNER if Path(db).name.startswith("demo") else ""
    views = _views(sm, qs, manual_hours=manual_hours, token=token,
                   launchd=launchd)
    # 第一栏服务端就带 `on`。**不靠 JS 决定首屏显示哪一栏** —— JS 挂了的话
    # 「全部 view 都是 display:none」会渲染成一张纯白页，而纯白页看起来像
    # 「这个库是空的」。CSS 里 `.view:target` 是第二层兜底：没有 JS，点导航
    # 仍然能切栏（多显示一栏，不会少显示）。
    sections = "\n".join(
        f'<section class="view{" on" if i == 0 else ""}" id="{_e(slug)}">'
        f"{body}</section>"
        for i, (slug, _label, body) in enumerate(views))
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)}</title><style>{_CSS}</style></head><body><div class="wrap">
<h1>{_e(title)}</h1>
<p class="sub">调度 + 审计层：任务派给 coding agent，四道监工判收，
{len(GATE_CLAIMS)} 道机制闸门盯着「这份绿是不是真的」。数据源 <code>{_e(db)}</code>。</p>
{banner}
{_live_bar(sm, qs)}
{_nav(views)}
{sections}
</div><script>{_NAV_JS}{_LIVE_JS}</script></body></html>"""


# ---------- 服务 ----------

#: 固定环回地址。**刻意不做成参数** —— 理由见模块 docstring。
HOST = "127.0.0.1"

#: 单次表单提交的上限（字节）。口述需求几百字就够，给到 64K 是为了粘贴长文档。
MAX_POST = 64 * 1024


def launchd_status(label: str = "com.factory.loop") -> str:
    """launchd 里装了没有。页面上要显示这个，因为跑批不在网页上按。

    查不到就说「查不到」，不说「没装」—— 这台机器上 `launchctl` 可能因为
    权限或非 GUI 会话读不到列表，那和「用户没装」是两件事，处置也不一样。
    """
    import shutil
    import subprocess

    exe = shutil.which("launchctl")
    if not exe:
        return "查不到（这台机器上没有 launchctl）"
    try:
        p = subprocess.run([exe, "list"], capture_output=True, text=True,
                           timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"查不到（launchctl list 失败：{type(exc).__name__}）"
    if p.returncode != 0:
        return f"查不到（launchctl list 退出码 {p.returncode}）"
    if label in p.stdout:
        return f"已装（{label}）"
    return f"未装 —— 装法见 examples/launchd/{label}.plist"


def _origin_ok(headers) -> str:
    """跨 origin 的 POST 一律拒。返回空字符串 = 通过，否则是拒绝理由。

    为什么需要这个：本机浏览器上**任何**网页都能往 `127.0.0.1:8787` POST 一个
    表单（跨域限制拦的是读响应，不是发请求）。而这几个表单会花钱调模型、会往
    审计库写 resolution。所以判据是 fail-closed：Origin/Referer 只要不是
    我们自己，就拒 —— 包括「两个头都没有」的情况。curl 也会被拒，那没关系，
    命令行本来就该直接用 `factory prd`。
    """
    raw = headers.get("Origin") or headers.get("Referer") or ""
    if not raw:
        return "请求没有 Origin/Referer 头 —— 这些表单只接受本页面提交"
    host = urlparse(raw).hostname
    if host not in ("127.0.0.1", "localhost", "::1"):
        return f"Origin 是 {host}，不是本机页面 —— 拒绝"
    return ""


#: 表单能碰的动作。**白名单**，不是「凡 POST 都试着调一下」——
#: 后者会让 `/prd/../something` 之类的路径落进一个我们没想过的分支。
ACTIONS = ("/prd", "/override", "/defect")


def perform(action: str, fields: dict[str, str], *, db: str | Path,
            queue: str | None, workspace: str | None = None,
            binary: str = "claude", propose_checks: bool = True) -> str:
    """跑一个表单动作，返回一段结果 HTML。

    做法是**造一个 Namespace 去调现有的 `_cmd_*`**，不在这里重写一遍逻辑：
    重写等于把闸门、审计写入、退出码约定各实现两遍，而两份实现漂移的方向
    不可预测 —— 「网页提的需求没过闸门」这种 bug 不会有任何报错。

    所有异常都抓住并渲染出来（fail-closed 的展示侧含义）：一个空白结果页和
    「入队成功」长得一样，而这两件事的后续动作完全相反。
    """
    import argparse
    import contextlib
    import io

    from factory import cli

    if action not in ACTIONS:
        return f'<p class="bad">不认识的动作 {_e(action)}</p>'
    if action == "/prd":
        if not queue:
            # 没有队列目录就没有「入队」这件事，而落到默认 tasks/ 目录会让
            # 页面说「已写入」却什么都没进队 —— 那是最难查的一种成功。
            return ('<p class="bad">没给 <code>--queue</code>，'
                    "网页提需求无处可入队。重启 dashboard 时带上它。</p>")
        ns = argparse.Namespace(
            text=fields.get("text", ""), text_file=None, audio=None,
            whisper_binary="whisper", whisper_model="small", language=None,
            binary=binary, intake_model="sonnet",
            split=True, split_model="haiku",
            dry_run=False, propose_checks=propose_checks,
            workspace=workspace, queue=str(queue), output=None,
        )
        fn = cli._cmd_prd
    elif action == "/override":
        ns = argparse.Namespace(db=str(db),
                                attempt_id=fields.get("attempt_id", ""),
                                resolution=fields.get("resolution", ""))
        fn = cli._cmd_override
    else:
        ns = argparse.Namespace(db=str(db),
                                attempt_id=fields.get("attempt_id") or None,
                                defect_id=fields.get("defect_id", ""),
                                commit=fields.get("commit") or None)
        fn = cli._cmd_defect

    out, err = io.StringIO(), io.StringIO()
    try:
        # attempt_id 在 CLI 里是 int（argparse 转的），网页表单给的是字符串。
        # 在这里转而不是让底下崩：崩了的话页面上只有一句 ValueError，
        # 看的人不知道是自己填错了还是系统坏了。
        if action in ("/override", "/defect") and ns.attempt_id is not None:
            if str(ns.attempt_id).strip() == "":
                ns.attempt_id = None
            else:
                ns.attempt_id = int(str(ns.attempt_id).strip())
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = fn(ns)
    except Exception as exc:  # noqa: BLE001
        return (f'<p class="bad">{_e(action)} 抛异常了 —— '
                f"{_e(type(exc).__name__)}: {_e(exc)}</p>"
                f"<pre>{_e(out.getvalue() + err.getvalue())}</pre>")
    # 退出码**必须显示**：3 是「闸门拦下了」，它是正常工作而不是错误，
    # 但也绝不能和 0 一样显示成绿的。
    verdict = {0: ("ok", "成功"), 3: ("warn", "被闸门拦下（这是闸门在正常工作）")
               }.get(code, ("bad", f"失败（退出码 {code}）"))
    return (f'<p class="{verdict[0]}"><b>{_e(action)} → {_e(verdict[1])}</b></p>'
            f"<pre>{_e(out.getvalue())}{_e(err.getvalue())}</pre>")


def _hours(path: str) -> float:
    """从 `?manual_hours=4` 取那个人工假设。取不到就用默认值。

    非法值静默回落到默认，不报错：这个参数只影响一个「假设」栏的算术，
    为它把整页 500 掉是不成比例的。负数也回落 —— 一个负的「省下的人力」
    在页面上是纯噪音。
    """
    from urllib.parse import parse_qs

    raw = parse_qs(urlparse(path).query).get("manual_hours", [""])[0]
    try:
        v = float(raw)
    except ValueError:
        return MANUAL_HOURS
    return v if 0 < v <= 1000 else MANUAL_HOURS


def serve(db: str | Path, *, port: int = 8787, task_id: str | None = None,
          queue: str | None = None, workspace: str | None = None,
          binary: str = "claude") -> int:
    """起一个只读的本地服务。每次请求重新读库，所以刷新就能看到新数据。

    每请求重读而不是启动时缓存：这一页的主要用途之一是「盯着正在跑的批」，
    缓存会让它显示一个不再为真的世界，而那种错误在页面上完全看不出来。
    库是 SQLite、只读、单人看，重读的代价可以忽略。

    「只读」到这一版有了例外：三个 POST 表单。它们仍然只是 CLI 的壳
    （见 `perform`），且受两道限制 —— 进程内随机 token + Origin 检查。
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    # 进程内随机 token，不落盘、不进环境变量。重启 dashboard 就换一个 ——
    # 那正是想要的：一个存起来的 token 会被别的页面拿去用。
    token = secrets.token_urlsafe(24)
    launchd = launchd_status()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _page(self, extra: str = "") -> bytes:
            try:
                sm = collect(db, task_id=task_id)
                qs = queue_state(queue)
                page = render(sm, db=str(db), qs=qs, token=token,
                              launchd=launchd,
                              manual_hours=_hours(self.path))
                if extra:
                    page = page.replace("<h1>", extra + "<h1>", 1)
            except Exception as exc:  # noqa: BLE001
                # 读库失败要**显示出来**，不能渲染成一张空表 ——
                # 空表和「库里没数据」长得一模一样。
                page = (f"<!doctype html><meta charset=utf-8>"
                        f"<h1>读 {_e(db)} 失败</h1><pre>{_e(exc)}</pre>")
            return page.encode("utf-8")

        def do_GET(self) -> None:  # noqa: N802 - stdlib 要求这个名字
            if self.path.startswith("/health"):
                self._send(b'{"ok":true}', "application/json; charset=utf-8")
            elif self.path.startswith("/state.json"):
                try:
                    payload = state_payload(collect(db, task_id=task_id),
                                            queue_state(queue))
                except Exception as exc:  # noqa: BLE001
                    # 这里也不许返一个空 JSON：轮询脚本会把它当成「一切归零」，
                    # 于是页面上的数字全部掉到 0，看着像批跑完了。
                    payload = {"error": f"{type(exc).__name__}: {exc}"}
                self._send(json.dumps(payload, ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
            else:
                self._send(self._page(), "text/html; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802 - stdlib 要求这个名字
            from urllib.parse import parse_qs

            action = urlparse(self.path).path
            bad = _origin_ok(self.headers)
            if bad:
                self._send(self._page(f'<p class="banner">{_e(bad)}</p>'),
                           "text/html; charset=utf-8", 403)
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n > MAX_POST:
                self._send(self._page(
                    f'<p class="banner">提交太大（{n} 字节 > {MAX_POST}）</p>'),
                    "text/html; charset=utf-8", 413)
                return
            raw = self.rfile.read(n).decode("utf-8", "replace")
            fields = {k: v[0] for k, v in parse_qs(raw).items()}
            # 比长度再比内容：`compare_digest` 对长度不同的输入本来就返回
            # False，但先判空能让「表单里根本没有 token 字段」有明确的提示。
            got = fields.get("token", "")
            if not got or not secrets.compare_digest(got, token):
                self._send(self._page(
                    '<p class="banner">表单 token 不对 —— dashboard 重启过的话'
                    "刷新这一页再提交。</p>"),
                    "text/html; charset=utf-8", 403)
                return
            result = perform(action, fields, db=db, queue=queue,
                             workspace=workspace, binary=binary)
            self._send(self._page(f'<div class="banner">{result}</div>'),
                       "text/html; charset=utf-8")

        def log_message(self, *a) -> None:
            """默认会把每个请求打到 stderr，和 loop 的日志混在一起。"""

    with ThreadingHTTPServer((HOST, port), Handler) as srv:
        print(f"dashboard: http://{HOST}:{port}   (Ctrl-C 停)")
        print(f"  数据源 : {db}")
        print(f"  队列   : {queue or '（没给 --queue，树没有依赖边，也不能提需求）'}")
        print(f"  launchd: {launchd}")
        print("  只绑环回地址，没有认证 —— 要给远程的人看请用 "
              "`factory dashboard --once out.html` 导出后发文件")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\n停了")
    return 0


def export(db: str | Path, out: str | Path, *, task_id: str | None = None,
           queue: str | None = None) -> int:
    """导出一份自包含的静态 HTML。发给别人比让他们连你的端口安全。

    不传 token，所以导出的页面里**没有表单** —— 一份发出去的 HTML 上摆着
    「提需求」按钮，按下去只会静默失败，那比没有这个按钮更糟。
    """
    path = Path(out)
    page = render(collect(db, task_id=task_id), db=str(db),
                  qs=queue_state(queue))
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

#: 同一个任务的三轮：被打回两次，第三轮才过。
#:
#: 单独列出来是因为 `_DEMO_ROWS` 是一 task 一行，而演示要讲的核心之一是
#: **往复**（编码→测试→打回→再编码）。只有一轮的示例数据在树上每个节点都只挂
#: 一行，看起来像「一次就过」—— 那不是这套系统实际的工作方式，用它去演示是
#: 在展示一个不存在的顺利。
_DEMO_ROUNDS = (
    (Resolution.REWORKED, 0.51,
     ("shadow-code", "新增 src/retry.py 被 .gitignore 挡住 —— check 却在跑它")),
    (Resolution.REWORKED, 0.44,
     ("fake-green", "金丝雀注入后测试仍退出 0 —— 这份绿推不翻")),
    (Resolution.MERGED, 0.39, None),
)

#: 示例队列的依赖边：(文件名 stem, 落在哪个目录, 前置)。
#:
#: 树边只存在于队列 YAML 里，审计库没有这个字段。所以 `--demo` 不造队列的话，
#: 演示页上那棵树是一排平铺的根 —— 而「真依赖」正是这一版要展示的东西。
_DEMO_QUEUE = (
    ("T-101", DONE, ()),
    ("T-102", DONE, ("T-101",)),
    ("T-110", DONE, ("T-101",)),
    ("T-103", INBOX, ("T-102",)),
    ("T-104", INBOX, ("T-103",)),
    ("T-105", RUNNING, ("T-102",)),
    ("T-106", NEEDS_HUMAN, ()),
    ("T-107", INBOX, ("T-106",)),
    ("T-109", BLOCKED, ()),
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
    _demo_rounds(store)
    return str(path)


def _demo_rounds(store: AuditStore) -> None:
    """T-110：一个任务被打回两轮、第三轮合并。走真实写入路径，attempt_no 由
    `open_attempt` 自己数（它按 task_id 递增），所以顺序是靠**插入顺序**成立的。
    """
    for resolution, cost, gate in _DEMO_ROUNDS:
        aid = store.open_attempt(
            task_id="T-110", spec_ref=["§4"], oracle_class=OracleClass.A,
            class_reason="有可执行判据：pytest tests/test_retry.py",
            harness="claude-code", harness_version="2.1.0",
            model="claude-opus-5",
        )
        store.record_result(
            aid, diff_hash=f"{abs(hash((cost, gate))):08x}"[:8], commit=None,
            transcript_path="/tmp/transcripts/T-110.jsonl",
            tokens_in=21_000, tokens_out=5_100, cost_usd=cost,
            wall_clock_ms=140_000,
        )
        for role in (SupervisorRole.REGRESSION, SupervisorRole.SPEC,
                     SupervisorRole.RISK, SupervisorRole.ARCHITECTURE):
            fired = bool(gate) and role is SupervisorRole.REGRESSION
            store.record_verdict(
                aid, role=role,
                verdict=Verdict.FAIL if fired else Verdict.PASS,
                claims=([{"check": gate[0], "command": "",
                          "expected": "这一轮没动过", "got": gate[1]}]
                        if fired else []),
                tokens=1_800, cost_usd=0.012)
        store.finalize(aid, resolution)


def build_demo_queue(root: str | Path = "demo-queue") -> str:
    """造一份示例队列目录，让树有真的依赖边。

    写的是**真的 Backlog 目录结构**（`Backlog.ensure()` 建的那几个目录），
    不是一个假的 dict：树的读取路径要和真实队列完全一致，否则演示页上好看的
    那棵树在真实队列上可能根本读不出来。

    `running/` 里那个条目**故意不带 `.claim`**：它只用来让页面上「正在跑」
    那个数字非零，而 `recover()` 会把没 claim 的条目当崩溃残留搬走 —— 所以
    这个目录只适合看，不适合真的 `factory loop` 指过来。目录名带 demo 是提醒。
    """
    from factory.backlog.store import Backlog

    path = Path(root)
    if not path.name.startswith("demo"):
        raise ValueError(
            f"示例队列目录必须以 demo 开头（给的是 {path.name}）—— "
            "它会被 loop 当成真队列，而里面的条目是编的。")
    bl = Backlog(path).ensure()
    for name, state, deps in _DEMO_QUEUE:
        doc = {
            "task_id": name,
            "prompt": f"（示例）{name} 的需求描述",
            "acceptance": "（示例）验收标准",
            "spec_ref": [f"§{name[-1]}"],
        }
        if deps:
            doc["depends_on"] = list(deps)
        (bl.dir(state) / f"{name}.yaml").write_text(
            yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
            encoding="utf-8")
    return str(path)

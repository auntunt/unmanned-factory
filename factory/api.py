"""只读 JSON API：把队列目录和审计库喂给前端。

旧 `dashboard.py` 是「读库 + 渲染 HTML」一体的。前端拆出去之后这一层只剩
前半截：读队列、读 audit.db、序列化成 JSON。**一个字节都不往两个数据源里写**
（POST/PUT/DELETE 根本没实现，见文件末尾的 `Handler`）。

## 为什么不引 flask/fastapi

和旧 dashboard 同一个取舍：项目生产依赖只有 SQLAlchemy 和 PyYAML。三个只读
GET 端点、单人看、无并发压力，标准库 `http.server` 够用。为这个把 web 框架
拖进生产依赖，代价不划算。

## 为什么数据组装是纯函数

`list_tasks` / `task_detail` / `global_stats` 不碰 `self`、不碰 socket、
不知道 HTTP 存在。HTTP 层（`serve_api`）只做三件事：路由、序列化、贴 CORS 头。
这样测试不用起服务器、不用抢端口、不用 sleep 等启动 —— 一次断言就是一次函数
调用。形状回归（前端按 `contracts/api-shape.md` 写死了类型）能在毫秒级测出来。

## 为什么绑 127.0.0.1 且没有认证

审计库里有 prompt、监工 claims、transcript 路径 —— 够拼出「这个仓库在做什么、
哪里被拦下过」。所以和旧 dashboard 一样：bind 固定 127.0.0.1，**不提供
`--host` 参数**。给了 `--host 0.0.0.0` 这个口子，它就会出现在某份 README、
某个 tmux 快捷键里，然后在咖啡馆的 WiFi 上开着。外部访问走 Caddy 反代，
那条路上有认证，而这里一点都没有。

CORS 是 `*`：前端 dev server 在另一个端口（5173）上，跨端口就是跨源。
放开它不放开 bind —— 能连上 127.0.0.1:8788 的人本来就能直接 curl。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

import yaml

from factory.audit.models import NOT_DISPATCHED
from factory.audit.store import AuditStore
from factory.backlog.store import BLOCKED, DONE, INBOX, NEEDS_HUMAN, RUNNING, STATES

#: 只绑环回。刻意没有 host 参数，理由见模块 docstring。
HOST = "127.0.0.1"

#: 队列目录名 → JSON 里的桶名。`needs-human` 带连字符，JS 里 `d.needs-human`
#: 是个减法表达式 —— 所以 API 边界上统一成下划线。映射写死一张表而不是
#: `replace("-", "_")`：前端类型（QueueState）按这五个字面量写死了，
#: 用字符串变换的话队列层改个目录名会静默改掉 API 的键，前端拿到 undefined。
BUCKETS: dict[str, str] = {
    INBOX: "inbox",
    RUNNING: "running",
    DONE: "done",
    NEEDS_HUMAN: "needs_human",
    BLOCKED: "blocked",
}

#: `prompt_preview` 的长度上限。契约（api-shape.md）写死 60 + 省略号。
PREVIEW_CHARS = 60

#: 队列撞名后缀（`T-x.2.yaml`）。和 backlog/store.py 的 `_PARK_SUFFIX` 是同一
#: 件事，刻意不 import 它 —— 那是私有名字，import 私有名会在对面改名时静默
#: 失配。这里失配的后果是同一个任务的两次归档在前端显示成两个任务（看得见），
#: 不是判决出错。
_PARK_SUFFIX = re.compile(r"\.\d+$")


class QueueUnreadable(RuntimeError):
    """队列根目录不存在 / 不是目录 / 里面没有状态子目录。

    **抛异常而不是返回五个空桶**：读不到队列和队列是空的，在前端长得一模一样
    —— 而前者意味着这一页在说谎。路径写错、目录被删、指到一个文件上，都需要
    人动手；一个空队列是安静的，静默降级会把这三种情况全渲染成「今天没任务」。
    HTTP 层把它转成 500 + `{"error": ...}`，前端能把原因显示出来。
    """


def _queue_root(queue: str | Path) -> Path:
    """校验并返回队列根目录。

    判据取「STATES 里有没有任何一个子目录存在」而不是「根目录在不在」：
    一个刚 `ensure()` 过的空队列几个子目录都在、条目为零，那是真的空，
    该正常返回五个空桶。沿用旧 dashboard `queue_state()` 的两段判法。
    """
    root = Path(queue).expanduser()
    if not root.is_dir():
        raise QueueUnreadable(f"{root} 不是一个目录（路径写错了？）")
    if not any((root / s).is_dir() for s in STATES):
        raise QueueUnreadable(
            f"{root} 下面没有 inbox/running/done… 这些目录，不像一个队列"
            "（路径写错了？还是没跑过 factory prd --queue？）"
        )
    return root


def _entries(d: Path) -> list[Path]:
    """一个状态目录下的任务条目，按文件名排序。

    只认 `.yaml` / `.yml`：旁文件（`.claim`、`.result.json`）和杂物不是队列条目。
    判据和 `backlog.store._is_task` 一致，但不 import 它（同样是私有名）。
    """
    if not d.is_dir():
        return []
    return sorted(
        (p for p in d.iterdir() if p.is_file() and p.suffix in (".yaml", ".yml")),
        key=lambda p: p.name,
    )


def _read_task_yaml(path: Path) -> dict:
    """读一份任务 YAML。读不出来返回空 dict，**不抛**。

    和队列根目录的处理刻意相反：根目录读不出来是「整个数据源没了」，必须炸；
    单个条目的 YAML 坏了只影响它自己那一行，把整个列表带崩的话，一个手改坏的
    文件会让前端整页白屏。坏条目仍然出现在列表里（文件名就是身份），只是
    prompt 为空 —— 看得见，且能点进去。
    """
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _preview(text: str) -> str:
    """prompt 的一行摘要：压掉换行，截 60 字符，超了加省略号。

    截断在后端做而不是让前端 CSS 省略号处理：整段 prompt 可能是几 KB，
    列表页有几十个任务，把全文塞进列表响应等于每次刷新都传一遍所有正文。
    """
    flat = " ".join(str(text or "").split())
    return flat[:PREVIEW_CHARS] + "…" if len(flat) > PREVIEW_CHARS else flat


def _mtime_iso(path: Path) -> str:
    """条目的修改时间，ISO 8601。取不到返回空字符串（键仍然存在）。"""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat(
            timespec="seconds")
    except OSError:
        return ""


def _ident(path: Path) -> str:
    """条目的任务身份 = 文件名 stem 剥掉 `.N` 撞名后缀。

    用文件名而不是 YAML 里的 `task_id`：一个读 YAML 就崩的条目正是最该被看见
    的那种，而在这里读 YAML 会让它从列表里消失。队列层的身份判法（`merged_ids`）
    也是文件名，两边保持同一个判据。
    """
    return _PARK_SUFFIX.sub("", path.stem)


def _checks(doc: dict) -> list[dict]:
    """任务 YAML 里的 checks，规整成 `[{name, command}]`。

    只取这两个字段：`expect` / `timeout_s` 是派发器的输入，前端展示不需要，
    而 timeout_s 这种数字放进响应会让人以为它是「跑了多久」。
    """
    out = []
    for c in doc.get("checks") or ():
        if isinstance(c, dict):
            out.append({"name": str(c.get("name") or ""),
                        "command": str(c.get("command") or "")})
    return out


# ---------- 审计侧 ----------


def _resolution(attempt) -> str:
    """attempt 的 resolution 字符串，预分级拦下的记成 `not_dispatched`。

    判据落在 **harness_version** 上，不是 harness —— dispatcher 记预派发拦截时
    harness 照样填 adapter 名字（那确实是那个 adapter 的任务），只把 version
    置成 `NOT_DISPATCHED`。读错字段的后果是 D 类硬闸门在前端显示成
    「pending 中」，而那些轮次从没调过 harness、永远不会有结果。
    判据跟 metrics.py / 旧 dashboard 的 `dispatched` 保持同一个。
    """
    if attempt.harness_version == NOT_DISPATCHED:
        return "not_dispatched"
    return str(attempt.resolution)


def _attempt_json(attempt, permission_events: tuple = ()) -> dict:
    """一轮 attempt 的完整 JSON，含监工裁决和权限门事件。

    `wall_clock_s` 是秒（ms / 1000），单位换算在后端做：契约里写死了这件事，
    让前端除 1000 的话每个用到它的组件都得记住这个数，漏一处就差三个数量级。
    """
    return {
        "attempt_no": attempt.attempt_no,
        "model": attempt.model,
        "harness": attempt.harness,
        "harness_version": attempt.harness_version,
        "resolution": _resolution(attempt),
        "commit": attempt.commit,
        "cost_usd": round(attempt.cost_usd, 6),
        "tokens_in": attempt.tokens_in,
        "tokens_out": attempt.tokens_out,
        "wall_clock_s": round(attempt.wall_clock_ms / 1000, 1),
        "created_at": attempt.created_at.isoformat(timespec="seconds"),
        "verdicts": [
            {
                "role": str(v.role),
                "verdict": str(v.verdict),
                # claims 可能是 None（老行）—— 前端类型写的是数组，
                # 给 null 会让 `.map()` 直接抛。
                "claims": list(v.claims or ()),
            }
            for v in attempt.supervisors
        ],
        "permission_events": [
            {
                "tool": e.tool,
                "target": e.target,
                "decision": e.decision,
                "rule": e.rule,
                "reason": e.reason,
                "tokens": e.tokens,
                "cost_usd": round(e.cost_usd, 6),
            }
            for e in permission_events
        ],
    }


# ---------- 纯函数：三个端点的数据组装 ----------
#
# 这三个函数是 API 的全部业务逻辑。它们只吃路径和 task_id，只吐 dict/list，
# 不知道 HTTP 存在 —— 测试直接调它们，不起服务器。


def list_tasks(queue: str | Path, db: str | Path | None = None) -> dict:
    """五个状态桶，每桶一串任务摘要。

    `db` 可选：给了就为 done / needs_human / blocked 桶补上审计侧的数字
    （resolution / attempts_count / total_cost_usd）。不给就只有队列侧的信息 ——
    契约里 inbox / running 桶本来就没有这三个字段（还没跑过）。

    队列读不出来时抛 `QueueUnreadable`，不返回五个空桶，理由见那个异常的文档。
    """
    root = _queue_root(queue)
    costs: dict[str, list] = {}
    if db is not None:
        for a in AuditStore(db).all_attempts():
            costs.setdefault(a.task_id, []).append(a)

    out: dict[str, list[dict]] = {}
    for state, bucket in BUCKETS.items():
        rows: list[dict] = []
        for path in _entries(root / state):
            doc = _read_task_yaml(path)
            ident = _ident(path)
            row = {
                "task_id": ident,
                "prompt_preview": _preview(doc.get("prompt", "")),
                "checks_count": len(_checks(doc)),
                "mtime": _mtime_iso(path),
            }
            # 跑过的桶才带审计数字。inbox/running 里的任务没有 attempt，
            # 硬塞一个 `total_cost_usd: 0` 会让前端显示「花了 $0」——
            # 那和「真的免费跑完了」长得一样。缺字段则明确是「还没有」。
            if state in (DONE, NEEDS_HUMAN, BLOCKED) and ident in costs:
                attempts = sorted(costs[ident], key=lambda a: a.attempt_no)
                row["resolution"] = _resolution(attempts[-1])
                row["attempts_count"] = len(attempts)
                row["total_cost_usd"] = round(
                    sum(a.cost_usd for a in attempts), 6)
            rows.append(row)
        out[bucket] = rows
    return out


def _locate(root: Path, task_id: str) -> tuple[str | None, Path | None]:
    """任务在队列里的哪个状态目录。找不到返回 `(None, None)`。

    按 `BUCKETS` 的顺序找，第一个命中就返回。同一个任务同时出现在两个目录里
    是队列层的异常（正常流程是原子 rename），这里不报错也不合并 —— 前端显示
    第一个命中的状态，而 `list_tasks` 里两个桶都会有它，异常本身看得见。
    """
    for state in BUCKETS:
        for path in _entries(root / state):
            if _ident(path) == task_id:
                return state, path
    return None, None


def task_detail(db: str | Path, queue: str | Path, task_id: str) -> dict | None:
    """一个任务的全部：prompt 全文、checks、每一轮 attempt 和监工裁决。

    **找不到返回 None**，不抛异常 —— 由 HTTP 层转成 404。返回 None 而不是抛：
    「这个 id 不存在」是调用方给错了参数，属于正常的业务分支；用异常表达它会
    让 HTTP 层不得不用 try/except 做路由判断，而那条路上真正的故障
    （库坏了、盘满了）就会和「打错了一个字」混进同一个 except。

    判定「不存在」的依据是**队列和审计库都没有它**：任务归档后有人手动清理了
    队列条目，审计轨迹仍然在库里，那时详情页照样该打得开（`state` 为 None）。
    反过来刚入队还没跑的任务只在队列里，没有任何 attempt，也该打得开。
    """
    root = _queue_root(queue)
    state, path = _locate(root, task_id)
    store = AuditStore(db)
    attempts = store.attempts_for(task_id)
    if state is None and not attempts:
        return None

    doc = _read_task_yaml(path) if path is not None else {}
    # oracle_class / class_reason 取**最后一轮**：后分级比预分级更严时
    # dispatcher 会改写它（`escalate_class`），所以最新那一轮才是当前判定。
    last = attempts[-1] if attempts else None
    return {
        "task_id": task_id,
        "state": BUCKETS[state] if state is not None else None,
        "prompt": str(doc.get("prompt") or ""),
        "checks": _checks(doc),
        "oracle_class": str(last.oracle_class) if last else "",
        "class_reason": last.class_reason if last else "",
        "total_cost_usd": round(sum(a.cost_usd for a in attempts), 6),
        "total_wall_clock_s": round(
            sum(a.wall_clock_ms for a in attempts) / 1000, 1),
        # attempts_for() 已按 attempt_no 升序，契约要求的就是这个顺序。
        "attempts": [
            _attempt_json(a, store.permission_events_for(a.id))
            for a in attempts
        ],
    }


def global_stats(db: str | Path, queue: str | Path) -> dict:
    """整库汇总：任务数、轮次数、花费、按 resolution / 状态分布。

    `total_tasks` 数的是**审计库里出现过的 task_id**，不是队列条目数：队列条目
    做完会被清理，而报表要回答的是「这套东西一共跑过多少任务」。两个数在
    `by_state` 里各自可见，不合并成一个含糊的「任务数」。
    """
    attempts = AuditStore(db).all_attempts()
    by_task: Counter = Counter(a.task_id for a in attempts)
    total_tasks = len(by_task)
    total_cost = sum(a.cost_usd for a in attempts)

    counts = {}
    try:
        root = _queue_root(queue)
        for state, bucket in BUCKETS.items():
            counts[bucket] = len(_entries(root / state))
    except QueueUnreadable:
        # 这一个数拿不到不该把整份统计带崩：审计侧那半边（花费、轮次、
        # resolution 分布）仍然是有效信息。但**不填 0** —— 五个 0 会显示成
        # 「队列是空的」。给 None，前端渲染成 `—`。
        counts = dict.fromkeys(BUCKETS.values(), None)

    return {
        "total_tasks": total_tasks,
        "total_attempts": len(attempts),
        "total_cost_usd": round(total_cost, 6),
        "by_resolution": dict(Counter(_resolution(a) for a in attempts)),
        "by_state": counts,
        # 一次都没跑过时返回 0 而不是 None：这两个数是「平均」，而 0 个任务的
        # 平均花费确实是 0，没有歧义（合并率那种「0% 像是全失败」的问题在这里
        # 不存在）。除零仍然要防。
        "avg_cost_per_task_usd": (round(total_cost / total_tasks, 6)
                                  if total_tasks else 0),
        "avg_attempts_per_task": (round(len(attempts) / total_tasks, 2)
                                  if total_tasks else 0),
    }


# ---------- HTTP 层 ----------
#
# 这一层只做三件事：解析路径、调上面那三个纯函数、序列化。没有任何业务判断 ——
# 「任务存不存在」由 `task_detail` 返 None 表达，这里只负责把它翻成 404。


def route(path: str, *, db: str | Path, queue: str | Path) -> tuple[int, dict]:
    """把一个 URL 路径解析成 `(状态码, 响应体)`。

    单独抽出来而不是写在 `do_GET` 里：路由表本身值得被测（404 的形状、
    task_id 的 URL 解码、异常转 500），而测 `do_GET` 就得起服务器。
    """
    clean = urlparse(path).path.rstrip("/") or "/"
    try:
        if clean == "/api/tasks":
            return 200, list_tasks(queue, db)
        if clean == "/api/stats":
            return 200, global_stats(db, queue)
        if clean.startswith("/api/task/"):
            # unquote：task_id 里出现 `/` 或中文时地址栏里是百分号编码的。
            task_id = unquote(clean[len("/api/task/") :])
            if not task_id:
                return 404, {"error": "缺少 task_id"}
            detail = task_detail(db, queue, task_id)
            if detail is None:
                return 404, {"error": f"task not found: {task_id}"}
            return 200, detail
        if clean == "/api/health":
            return 200, {"ok": True}
    except QueueUnreadable as exc:
        # 500 而不是 200 + 空数据：读不到队列时前端必须知道这一页在说谎。
        return 500, {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        # 库坏了、盘满了、SQLAlchemy 抛了。把类型带上 —— 只有一句话的
        # error 在排查时等于没有。
        return 500, {"error": f"{type(exc).__name__}: {exc}"}
    return 404, {"error": f"no such endpoint: {clean}"}


def route_post(path: str, data: dict, *, queue: str | Path) -> tuple[int, dict]:
    """处理 POST 请求路由"""
    clean = urlparse(path).path.rstrip("/") or "/"
    try:
        if clean == "/api/submit":
            return submit_task(data, queue)
    except Exception as exc:  # noqa: BLE001
        return 500, {"error": f"{type(exc).__name__}: {exc}"}
    return 404, {"error": f"no such endpoint: {clean}"}


def submit_task(data: dict, queue: str | Path) -> tuple[int, dict]:
    """投递任务到 inbox"""
    task_id = data.get("task_id", "").strip()
    yaml_content = data.get("yaml", "").strip()

    # 验证
    if not task_id:
        return 400, {"error": "task_id 不能为空"}
    if not yaml_content:
        return 400, {"error": "yaml 内容不能为空"}
    # 报错指名道姓。含糊的「必须是 kebab-case」会让人对着
    # T-FED-updata-001 找不出问题在大写字母上。前端有同样的分级校验，
    # 这里是绕过前端直接 POST 时的同等待遇。
    if not re.match(r"^T-[a-z0-9-]+$", task_id):
        if not task_id.startswith("T-"):
            return 400, {"error": f"task_id 必须以 T- 开头，当前是 {task_id!r}"}
        # 只看 T- 之后：前缀那个 T 本来就该大写，连它一起判会把
        # T-fed_update 误报成「含大写字母 T」，还建议改成 t-fed_update
        # —— 把合法前缀也毁掉。
        rest = task_id[2:]
        if any(c.isupper() for c in rest):
            upper = "".join(sorted({c for c in rest if c.isupper()}))
            return 400, {
                "error": f"task_id 不能含大写字母（{upper}），"
                f"改成 T-{rest.lower()} 即可"
            }
        bad = "".join(sorted({c for c in rest if not re.match(r"[a-z0-9-]", c)}))
        if bad:
            return 400, {
                "error": f"task_id 含不允许的字符（{bad}），只能用小写字母、数字、连字符"
            }
        return 400, {"error": "task_id 的 T- 后面不能为空，如 T-fix-bug-123"}

    # 检查是否已存在
    queue_path = Path(queue)
    for state_dir in STATES:
        task_file = queue_path / state_dir / f"{task_id}.yaml"
        if task_file.exists():
            return 409, {"error": f"任务已存在于 {state_dir}/"}

    # 写入 inbox（用 open 而不是 write_text，避免某些环境下的权限问题）
    inbox_file = queue_path / INBOX / f"{task_id}.yaml"
    try:
        with open(inbox_file, "w", encoding="utf-8") as f:
            f.write(yaml_content)
        return 200, {"ok": True, "task_id": task_id, "file": str(inbox_file)}
    except Exception as exc:
        return 500, {"error": f"写入失败: {exc}"}


def serve_api(db: str | Path, queue: str | Path, *, port: int = 8788) -> None:
    """起一个只读 JSON API。每次请求重新读库，所以刷新就能看到新数据。

    只绑 127.0.0.1，没有认证 —— 理由见模块 docstring。只实现 GET 和
    OPTIONS（CORS 预检），POST/PUT/DELETE 根本没有对应方法，stdlib 会自动
    回 501。
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        # 默认是 HTTP/1.0，每个响应都关连接。前端轮询三个端点时那是三次
        # 握手，改成 1.1 让 keep-alive 生效（Content-Length 我们每次都写）。
        protocol_version = "HTTP/1.1"

        def _cors(self) -> None:
            """CORS 头集中一处。漏贴一个端点的话前端只看到一句语焉不详的
            network error，而后端日志里那次请求是 200 —— 排查方向会被带偏。"""
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def _json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - stdlib 要求这个名字
            code, payload = route(self.path, db=db, queue=queue)
            self._json(code, payload)

        def do_POST(self) -> None:  # noqa: N802 - stdlib 要求这个名字
            """处理投递任务请求"""
            try:
                # 读请求体
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                data = json.loads(body)
                
                # 路由到 submit 处理函数
                code, payload = route_post(self.path, data, queue=queue)
                self._json(code, payload)
            except json.JSONDecodeError:
                self._json(400, {"error": "Invalid JSON"})
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

        def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib 要求这个名字
            """CORS 预检。前端跨端口带自定义头时浏览器会先发这个。"""
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self._cors()
            self.end_headers()

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            """默认会把每个请求打到 stderr，和 loop 的日志混在一起。"""

    with ThreadingHTTPServer((HOST, port), Handler) as srv:
        print(f"api: http://{HOST}:{port}/api/tasks   (Ctrl-C 停)")
        print(f"  数据源 : {db}")
        print(f"  队列   : {queue}")
        print("  只读、只绑环回地址、没有认证 —— 外部访问走 Caddy 反代")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\n停了")

"""对话式控制台的 HTML 渲染层。

为什么不复用 dashboard.py 的 render()：那边是一张大页面，所有 tab 的 HTML
一次全生成。这边要支持 SSE 增量刷新 —— 得把单条 thread 渲染成独立片段，
服务端 push 时只推那一条，不推整页。复用 render() 就等于每次 SSE 推整个页面，
而整个页面可能有几千行，带宽和 DOM diff 都不合适。

所以这里自己管 HTML，只共享 CSS 里的 --bg/--fg/--card/--accent 等 token。
"""

from __future__ import annotations

import html
from factory.console import STAGES, STAGE_HINT, STAGE_LABEL, OK, WAIT, STOP, Step, Thread


# ── 颜色 / 图标 ────────────────────────────────────────────────
_TONE_ICON = {OK: "✓", WAIT: "→", STOP: "⚠"}
_TONE_CLS = {OK: "s-ok", WAIT: "s-wait", STOP: "s-stop"}


def _e(v: str) -> str:
    return html.escape(str(v), quote=True)


def _step_html(s: Step) -> str:
    icon = _TONE_ICON.get(s.tone, "·")
    cls = _TONE_CLS.get(s.tone, "s-wait")
    detail = f'<span class="step-detail">{_e(s.detail)}</span>' if s.detail else ""
    when = f'<span class="step-when">{_e(s.when)}</span>' if s.when else ""
    return (
        f'<li class="step {cls}">'
        f'{when}<span class="step-label">{_e(s.label)}</span>'
        f'<span class="step-text">{_e(s.text)}</span>'
        f'{detail}</li>'
    )


def _thread_html(t: Thread) -> str:
    tone_cls = _TONE_CLS.get(t.tone, "s-wait")
    icon = _TONE_ICON.get(t.tone, "→")
    steps_html = "\n".join(_step_html(s) for s in t.steps)
    said = (
        f'<details class="said"><summary>你说的原话</summary>'
        f'<pre>{_e(t.said.strip())}</pre></details>'
    ) if t.said and t.said.strip() != t.title.strip() else ""
    return f"""
<article class="thread {tone_cls}" id="t-{_e(t.thread_id)}">
  <header class="thread-hd">
    <span class="thread-icon">{icon}</span>
    <h3 class="thread-title">{_e(t.title)}</h3>
  </header>
  <ol class="steps">{steps_html}</ol>
  {said}
</article>"""


def _stage_bar_html() -> str:
    items = "".join(
        f'<li title="{_e(STAGE_HINT[k])}">{_e(label)}</li>'
        for k, label, _ in STAGES
    )
    return f'<ol class="stagebar">{items}</ol>'


# ── 完整页面 ───────────────────────────────────────────────────
_CSS = """\
:root{--bg:#fbfbfa;--fg:#1a1a18;--dim:#6b6b66;--line:#e3e3df;
--ok:#2f7d4f;--bad:#b3341f;--warn:#8a6d1f;--card:#fff;--accent:#2158c9}
@media(prefers-color-scheme:dark){:root{--bg:#111110;--fg:#e8e8e3;
--dim:#9a9a92;--line:#2e2e2a;--ok:#6bbb85;--bad:#e0765c;--warn:#d0ac52;
--card:#1e1e1b;--accent:#6f9dff}}
*{box-sizing:border-box;margin:0;padding:0}
body{font:15px/1.55 system-ui,sans-serif;background:var(--bg);color:var(--fg);
min-height:100dvh;display:flex;flex-direction:column}

/* ── 顶栏 ── */
.topbar{padding:.6rem 1.25rem;border-bottom:1px solid var(--line);
display:flex;align-items:center;gap:1rem;justify-content:space-between}
.topbar h1{font-size:1rem;font-weight:700;letter-spacing:-.01em}
.topbar a{font-size:.8rem;color:var(--dim);text-decoration:none}
.topbar a:hover{color:var(--accent)}

/* ── 阶段指示条 —— 横铺在顶部，让用户第一眼就知道任务要经几步 ── */
.stagebar{list-style:none;display:flex;gap:0;
border-bottom:1px solid var(--line);overflow-x:auto;padding:0 1rem}
.stagebar li{padding:.5rem .75rem;font-size:.72rem;color:var(--dim);
white-space:nowrap;cursor:default;border-bottom:2px solid transparent;
transition:color .15s}
.stagebar li:hover{color:var(--fg)}

/* ── 主体 ── */
.layout{flex:1;display:grid;grid-template-columns:1fr;max-width:860px;
width:100%;margin:0 auto;padding:1.25rem}

/* ── 提需求区 ── */
.compose{background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:1rem;margin-bottom:1.25rem;
box-shadow:0 1px 3px rgba(0,0,0,.06)}
.compose p.hint{font-size:.78rem;color:var(--dim);margin-bottom:.6rem}
.compose textarea{width:100%;min-height:72px;font:inherit;font-size:.9rem;
padding:.55rem .65rem;border:1px solid var(--line);border-radius:6px;
background:var(--bg);color:var(--fg);resize:vertical;outline:none}
.compose textarea:focus-visible{border-color:var(--accent);
box-shadow:0 0 0 2px color-mix(in srgb,var(--accent) 20%,transparent)}
.compose .row{display:flex;gap:.6rem;margin-top:.6rem;align-items:center}
.compose .row button{font:inherit;font-weight:600;font-size:.88rem;
padding:.45rem 1rem;background:var(--accent);color:#fff;
border:none;border-radius:6px;cursor:pointer;transition:opacity .15s}
.compose .row button:hover{opacity:.88}
.compose .row button:disabled{opacity:.45;cursor:not-allowed}
.compose .row .note{font-size:.75rem;color:var(--dim)}

/* ── 任务列表 ── */
.threads{display:flex;flex-direction:column;gap:.85rem}
.empty{color:var(--dim);font-size:.88rem;padding:1.5rem 0;text-align:center}

/* ── 单条 Thread 卡片 ── */
.thread{background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:1rem 1.1rem;
box-shadow:0 1px 3px rgba(0,0,0,.06);transition:border-color .2s}
.thread.s-stop{border-left:3px solid var(--bad)}
.thread.s-ok{border-left:3px solid var(--ok)}
.thread.s-wait{border-left:3px solid var(--accent)}
.thread-hd{display:flex;align-items:baseline;gap:.55rem;margin-bottom:.6rem}
.thread-icon{font-size:1rem;flex-shrink:0}
.thread.s-ok .thread-icon{color:var(--ok)}
.thread.s-stop .thread-icon{color:var(--bad)}
.thread.s-wait .thread-icon{color:var(--accent)}
.thread-title{font-weight:600;font-size:.95rem;line-height:1.3}

/* ── 步骤列表 ── */
.steps{list-style:none;display:flex;flex-direction:column;gap:.25rem;
padding-left:.25rem}
.step{display:grid;grid-template-columns:4.5rem 6rem 1fr;
gap:.25rem .5rem;font-size:.8rem;align-items:baseline}
.step-when{color:var(--dim);font-variant-numeric:tabular-nums;font-size:.72rem}
.step-label{font-weight:600;color:var(--dim)}
.step.s-ok .step-label{color:var(--ok)}
.step.s-stop .step-label{color:var(--bad)}
.step.s-wait .step-label{color:var(--accent)}
.step-text{color:var(--fg)}
.step-detail{grid-column:3;color:var(--dim);font-size:.75rem;
padding:.1rem .5rem;border-left:2px solid var(--line);margin-top:.1rem}

/* ── 原话折叠 ── */
.said{margin-top:.6rem;font-size:.78rem}
.said summary{color:var(--dim);cursor:pointer;user-select:none}
.said pre{margin-top:.4rem;padding:.5rem .65rem;
background:var(--bg);border:1px solid var(--line);border-radius:5px;
font-family:inherit;white-space:pre-wrap;word-break:break-word;
color:var(--dim)}

/* ── 状态条 ── */
#status{font-size:.75rem;color:var(--dim);text-align:center;
padding:.5rem;border-top:1px solid var(--line)}
"""

_JS = """\
/* 提交需求：POST /console/prd，然后开 SSE 轮询刷新 */
const form = document.querySelector('.compose');
const ta   = form.querySelector('textarea');
const btn  = form.querySelector('button');
const note = form.querySelector('.note');
const list = document.querySelector('.threads');
const stat = document.getElementById('status');

function setStatus(msg){ stat.textContent = msg; }

/* SSE 自动刷新 —— 后端每 5 秒 push 一次最新的 thread HTML */
let es = null;
function startSSE(){
  if(es){ es.close(); }
  es = new EventSource('/console/stream');
  es.onmessage = e => {
    if(e.data === 'ping') return;
    try {
      const {html: h, error: err} = JSON.parse(e.data);
      if(err){ setStatus('⚠ ' + err); return; }
      if(h !== undefined){
        list.innerHTML = h || '<p class="empty">暂时没有任务，用上面的输入框提第一个需求吧</p>';
      }
    } catch(_)
  };
  es.onerror = () => setStatus('连接断开，5 秒后重连…');
}
startSSE();

/* 提交 */
form.addEventListener('submit', async e => {
  e.preventDefault();
  const text = ta.value.trim();
  if(!text) return;
  btn.disabled = true;
  note.textContent = '正在提交…';
  try {
    const r = await fetch('/console/prd', {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded',
                'X-Requested-With': 'XMLHttpRequest'},
      body: 'text=' + encodeURIComponent(text)
    });
    const d = await r.json();
    if(d.ok){
      ta.value = '';
      note.textContent = d.msg || '已提交，等待处理中…';
    } else {
      note.textContent = '⚠ ' + (d.error || '提交失败');
    }
  } catch(err){
    note.textContent = '⚠ 网络错误：' + err.message;
  } finally {
    btn.disabled = false;
  }
});
"""


def page(threads: list[Thread], error: str = "") -> str:
    threads_html = "\n".join(_thread_html(t) for t in threads) if threads else ""
    placeholder = "" if threads else '<p class="empty">暂时没有任务，用上面的输入框提第一个需求吧</p>'
    err_banner = (
        f'<p style="color:var(--bad);font-size:.8rem;padding:.5rem 0">'
        f'⚠ {_e(error)}</p>'
    ) if error else ""
    return f"""<!doctype html>
<html lang="zh-Hans">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>自动工厂</title>
<style>{_CSS}</style></head>
<body>
<header class="topbar">
  <h1>🏭 自动工厂</h1>
  <a href="/">看审计看板</a>
</header>
{_stage_bar_html()}
<main class="layout">
  <section class="compose">
    <p class="hint">口语化说清要做什么就行，不用写技术文档——提交后会自动拆成任务、过闸门、排队执行。</p>
    <form method="post" action="/console/prd" onsubmit="return false;">
      <textarea name="text" placeholder="例如：给登录页加忘记密码入口，点了发重置邮件" autofocus></textarea>
      <div class="row">
        <button type="submit">提这个需求</button>
        <span class="note">提交后在下面实时看进度</span>
      </div>
    </form>
  </section>
  {err_banner}
  <section class="threads" id="threads-list">
    {threads_html or placeholder}
  </section>
</main>
<div id="status">连接中…</div>
<script>{_JS}</script>
</body></html>"""


def threads_fragment(threads: list[Thread]) -> str:
    """SSE push 的 HTML 片段：只有 thread 卡片，不是整页。"""
    return "\n".join(_thread_html(t) for t in threads)

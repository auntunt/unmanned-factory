#!/usr/bin/env python3
"""Zero-dependency checker/static preview builder for this handbook's Markdown subset.

Not a GitBook renderer. Supported: headings, paragraphs, code fences, tables,
flat lists, images, links and inline code. Never evaluates Markdown/HTML/scripts.
"""
import argparse
import base64
import mimetypes
import html
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r'(!?)\[([^\]]*)\]\(([^\s)]+)(?:\s+"[^"]*")?\)')


def pages():
    return [(label, path) for image, label, path in LINK.findall((ROOT / 'SUMMARY.md').read_text()) if not image]


def slug(s):
    return re.sub(r'[^\w\-\u4e00-\u9fff]+', '-', s.lower()).strip('-')


def inline(s):
    tokens = []
    def token(value):
        tokens.append(value)
        return '\x00' + str(len(tokens) - 1) + '\x00'
    s = re.sub(r'`([^`]+)`', lambda m: token('<code>' + html.escape(m[1]) + '</code>'), s)
    def link(m):
        image, label, url = m.groups()
        parsed = urlsplit(url)
        if parsed.scheme and parsed.scheme not in ('http', 'https'):
            raise ValueError('Unsupported link scheme')
        if not parsed.scheme and parsed.path.endswith('.md'):
            url = ('index.html' if parsed.path == 'README.md' else parsed.path[:-3] + '.html') + (('#' + parsed.fragment) if parsed.fragment else '')
        safe = html.escape(url, quote=True)
        if image:
            return token('<figure><a href="' + safe + '" aria-label="放大图像"><img loading="lazy" src="' + safe + '" alt="' + html.escape(label, quote=True) + '"></a><figcaption>' + html.escape(label) + '</figcaption></figure>')
        return token('<a href="' + safe + '">' + html.escape(label) + '</a>')
    s = LINK.sub(link, s)
    s = html.escape(s)
    s = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', s)
    return re.sub(r'\x00(\d+)\x00', lambda m: tokens[int(m[1])], s)


def render(text):
    lines = text.splitlines()
    out, toc = [], []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1; continue
        if line.startswith('```'):
            language, code = line[3:].strip(), []
            i += 1
            while i < len(lines) and not lines[i].startswith('```'):
                code.append(lines[i]); i += 1
            out.append('<pre aria-label="' + html.escape(language or 'text') + '"><code>' + html.escape('\n'.join(code)) + '</code></pre>')
            i += 1; continue
        if m := re.match(r'^(#{1,4}) (.+)$', line):
            n, title = len(m[1]), m[2]
            anchor = slug(title)
            out.append(f'<h{n} id="{anchor}">{inline(title)}</h{n}>')
            if n in (2, 3): toc.append((title, anchor))
            i += 1; continue
        if line.startswith('|'):
            rows = []
            while i < len(lines) and lines[i].startswith('|'):
                cells = [x.strip() for x in lines[i].strip().strip('|').split('|')]
                if not all(re.fullmatch(r':?-+:?', x) for x in cells): rows.append(cells)
                i += 1
            out.append('<div class="table-scroll" tabindex="0" role="region" aria-label="可横向滚动的表格"><table>')
            for r, row in enumerate(rows):
                out.append('<tr>' + ''.join(('<th scope="col">' if r == 0 else '<td>') + inline(x) + ('</th>' if r == 0 else '</td>') for x in row) + '</tr>')
            out.append('</table></div>'); continue
        if re.match(r'^(?:- |\d+\. )', line):
            ordered = bool(re.match(r'^\d+\. ', line)); tag = 'ol' if ordered else 'ul'
            out.append('<' + tag + '>')
            while i < len(lines) and re.match(r'^(?:- |\d+\. )', lines[i]):
                out.append('<li>' + inline(re.sub(r'^(?:- |\d+\. )', '', lines[i])) + '</li>'); i += 1
            out.append('</' + tag + '>'); continue
        if line.startswith('!['):
            out.append(inline(line)); i += 1; continue
        paragraph = [line]; i += 1
        while i < len(lines) and lines[i].strip() and not re.match(r'^(?:#|```|\||!\[|- |\d+\. )', lines[i]):
            paragraph.append(lines[i]); i += 1
        out.append('<p>' + inline(' '.join(paragraph)) + '</p>')
    return '\n'.join(out), toc


CSS = '''
:root{--ink:#172b42;--muted:#53667c;--line:#dce5ed;--blue:#09599a;--paper:#fff;--wash:#f3f7fa}*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:90px}body{margin:0;color:var(--ink);background:var(--paper);font:16px/1.85 system-ui,-apple-system,"Noto Sans CJK SC","Microsoft YaHei",sans-serif}a{color:var(--blue);text-decoration-thickness:1px;text-underline-offset:3px}a:hover{color:#083965}a:focus-visible,summary:focus-visible{outline:3px solid #f2ad37;outline-offset:4px}.skip{position:absolute;left:16px;top:-100px;background:#fff;padding:12px;z-index:10}.skip:focus{top:8px}header{border-bottom:1px solid var(--line);padding:16px 28px;background:#102b46;color:#fff;display:flex;align-items:center;justify-content:space-between;gap:16px}header a{color:#fff;text-decoration:none;font-weight:750;font-size:20px}header small{color:#d4e3ef}nav{position:fixed;top:72px;bottom:0;left:0;width:260px;overflow:auto;padding:20px 16px 40px;background:var(--wash);border-right:1px solid var(--line)}nav a{display:block;padding:6px 12px;border-radius:5px;color:#314e69;text-decoration:none;font-size:14px}nav a[aria-current=page]{background:#ddebf6;color:#093f6b;font-weight:700;border-left:3px solid #1671b7}nav .nav-title{font-size:12px;font-weight:700;letter-spacing:.08em;color:var(--muted);padding:4px 12px 12px}main{max-width:1030px;margin-left:260px;padding:35px 54px 70px}h1{font-size:34px;line-height:1.3;letter-spacing:-.025em;margin:0 0 24px;color:#102b46}h2{font-size:23px;line-height:1.45;margin:36px 0 14px;padding-top:6px}h3{font-size:19px;margin:27px 0 10px}p{margin:14px 0}li{margin:7px 0}ul,ol{padding-left:1.6em}code{background:#edf2f6;border-radius:4px;padding:2px 5px;font:0.88em/1.6 ui-monospace,"Noto Sans Mono CJK SC",monospace;overflow-wrap:anywhere}pre{background:#122c43;color:#eef6fd;padding:20px;border-radius:8px;overflow:auto;font-size:14px;line-height:1.6}pre code{padding:0;background:none;overflow-wrap:normal;color:inherit}figure{margin:28px 0;background:#fff;border:1px solid var(--line);padding:20px 12px 14px;border-radius:9px;text-align:center}figure img{display:block;max-width:100%;height:auto;margin:0 auto;max-height:1000px;object-fit:contain}figcaption{font-size:13px;color:var(--muted);margin:12px 5px 0;text-align:center}.table-scroll{overflow-x:auto;margin:20px 0;border:1px solid var(--line);border-radius:7px}table{border-collapse:collapse;width:100%;font-size:14px;line-height:1.75;min-width:540px}th,td{padding:12px 14px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line)}th{background:#eaf2f8;font-weight:700}tr:last-child td{border-bottom:0}tr:nth-child(even) td{background:#fafcfd}.notice{font-size:13px;background:#f1f6fa;border-left:3px solid #577f9f;padding:11px 16px;margin:0 0 26px;color:#49637b}.pager{border-top:1px solid var(--line);margin-top:44px;padding-top:20px;display:flex;justify-content:space-between;gap:24px;font-size:14px}.mobile-nav{display:none}.toc{font-size:13px;margin:0 0 24px;padding:12px 16px;background:#f7f9fb;border-radius:6px}.toc summary{cursor:pointer;font-weight:700}.toc a{display:inline-block;margin:5px 18px 0 0}footer{color:var(--muted);font-size:12px;margin-top:30px}@media(min-width:1550px){main{margin-left:calc(260px + (100vw - 1550px)/2)}}@media(max-width:850px){header{padding:15px 18px;align-items:flex-start}header a{font-size:18px}header small{font-size:11px}nav{display:none}main{margin:0;padding:24px 20px 55px;max-width:none}.mobile-nav{display:block;background:var(--wash);padding:12px 20px;border-bottom:1px solid var(--line)}.mobile-nav summary{font-weight:700;cursor:pointer}.mobile-nav a{display:block;padding:6px}h1{font-size:28px}h2{font-size:22px}figure{padding:12px 7px}pre{padding:16px;font-size:12px}body{font-size:15px}.pager{font-size:13px;gap:15px}}@media print{nav,header,.mobile-nav,.toc,.pager{display:none}main{margin:0;padding:0;max-width:none}body{font-size:10pt}h1{font-size:24pt}h2{break-after:avoid;font-size:15pt}figure{break-inside:avoid}pre{white-space:pre-wrap;color:#000;background:#f3f5f7}a{color:#000}table{min-width:0;font-size:9pt}}
'''


def check():
    errors = []
    nav = pages(); targets = [p for _, p in nav]
    all_md = {p.name for p in ROOT.glob('*.md')} - {'SUMMARY.md'}
    if len(targets) != len(set(targets)): errors.append('Duplicate SUMMARY page')
    if set(targets) != all_md: errors.append('SUMMARY does not exactly cover all Markdown pages')
    image_count = 0
    for path in ROOT.glob('*.md'):
        text = path.read_text()
        if text.count('```') % 2: errors.append(f'{path.name}: unclosed code fence')
        headings = [slug(m[1]) for m in re.finditer(r'^#{1,4} (.+)$', text, re.M)]
        if len(headings) != len(set(headings)): errors.append(f'{path.name}: duplicate heading anchor')
        for image, label, url in LINK.findall(text):
            if image:
                image_count += 1
                if not label.strip(): errors.append(f'{path.name}: image lacks alt text')
            part = urlsplit(url)
            if part.scheme or part.netloc: continue
            dest = (path.parent / unquote(part.path)).resolve() if part.path else path
            if not dest.is_relative_to(ROOT): errors.append(f'{path.name}: link escapes handbook: {url}')
            elif not dest.exists(): errors.append(f'{path.name}: missing link: {url}')
            elif part.fragment and dest.suffix == '.md':
                anchors = {slug(m[1]) for m in re.finditer(r'^#{1,4} (.+)$', dest.read_text(), re.M)}
                if unquote(part.fragment) not in anchors: errors.append(f'{path.name}: missing anchor: {url}')
    for dot in (ROOT / 'diagrams').glob('*.dot'):
        for ext in ('png', 'svg'):
            if not (ROOT / 'assets' / (dot.stem + '.' + ext)).is_file(): errors.append('Missing diagram render: ' + dot.stem + '.' + ext)
    manifest = ROOT / 'source-map.json'
    if manifest.exists():
        repo = ROOT.parents[1]
        for entry in json.loads(manifest.read_text()):
            source = repo / entry['path']
            if not source.is_file(): errors.append('Missing source: ' + entry['path'])
            elif entry.get('symbol') and entry['symbol'] not in source.read_text(): errors.append('Missing source symbol: ' + entry['path'] + '::' + entry['symbol'])
    if errors:
        raise ValueError('\n'.join(errors))
    print(json.dumps({'pages': len(nav), 'image_references': image_count, 'diagrams': len(list((ROOT/'diagrams').glob('*.dot'))), 'local_links': 'passed', 'sources': 'passed'}, ensure_ascii=False))


def build(output=None):
    check()
    output = Path(output) if output else ROOT / '_preview'
    output.mkdir(parents=True, exist_ok=True)
    (output / 'style.css').write_text(CSS)
    for folder in ('assets', 'diagrams'):
        shutil.copytree(ROOT / folder, output / folder, dirs_exist_ok=True)
    nav = pages()
    for idx, (title, filename) in enumerate(nav):
        body, toc = render((ROOT / filename).read_text())
        links = ''.join('<a '+('aria-current="page" ' if p == filename else '')+'href="'+('index.html' if p == 'README.md' else p[:-3]+'.html')+'">'+html.escape(t)+'</a>' for t,p in nav)
        contents = '<details class="toc"><summary>本页内容</summary>' + ''.join('<a href="#'+a+'">'+html.escape(t)+'</a>' for t,a in toc) + '</details>'
        paging = []
        for j, label in ((idx-1, '上一页'), (idx+1, '下一页')):
            if 0 <= j < len(nav):
                t,p = nav[j]; href = 'index.html' if p == 'README.md' else p[:-3]+'.html'
                paging.append('<a href="'+href+'">'+label+' · '+html.escape(t)+'</a>')
            else: paging.append('<span></span>')
        doc = '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="color-scheme" content="light"><title>'+html.escape(title)+' · webuddy 维护人员手册</title><link rel="stylesheet" href="style.css"></head><body><a class="skip" href="#content">跳到正文</a><header><a href="index.html">webuddy 维护人员手册</a><small>本地离线版 · 2026.09<br>与同版本代码一起使用</small></header><nav aria-label="章节导航"><div class="nav-title">维护与恢复</div>'+links+'</nav><details class="mobile-nav"><summary>打开章节目录</summary>'+links+'</details><main id="content"><div class="notice">说明图由代码事实整理；本地预览不是 GitBook 官方渲染。未发布到线上。</div>'+contents+body+'<div class="pager">'+''.join(paging)+'</div><footer>维护提示：先保存现场，后做获授权处置；检查通过、发布成功和生产验收是不同证据。</footer></main></body></html>'
        (output / ('index.html' if filename == 'README.md' else filename[:-3]+'.html')).write_text(doc)
    # Directly linked support/source files remain available offline.
    for file in ROOT.iterdir():
        if file.is_file() and file.suffix in ('.json', '.md'):
            shutil.copy2(file, output / file.name)
    shutil.copytree(ROOT / 'tools', output / 'tools', dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__'))
    print('Built static preview:', output)


def single(output):
    """A single HTML file with inline CSS/images and chapter anchors for mobile."""
    check()
    output = Path(output)
    nav = pages()
    names = {('index.html' if p == 'README.md' else p[:-3]+'.html'): 'chapter-'+p[:-3].lower() for _,p in nav}
    nav_html = ''.join('<a href="#'+names['index.html' if p == 'README.md' else p[:-3]+'.html']+'">'+html.escape(t)+'</a>' for t,p in nav)
    sections = []
    for title, filename in nav:
        chapter = names['index.html' if filename == 'README.md' else filename[:-3]+'.html']
        body, _ = render((ROOT / filename).read_text())
        body = re.sub(r'<(/?)h([1-4])([^>]*)>', lambda m: '<'+m[1]+'h'+str(int(m[2])+1)+m[3]+'>', body)
        body = re.sub(r'id="([^"]+)"', lambda m: 'id="'+chapter+'-'+m[1]+'"', body)
        def url_rewrite(match):
            attr, url = match.groups()
            parsed = urlsplit(html.unescape(url))
            if parsed.scheme or parsed.netloc: return match[0]
            if parsed.path in names:
                new = '#'+names[parsed.path]+(('-'+parsed.fragment) if parsed.fragment else '')
            elif not parsed.path and parsed.fragment:
                new = '#'+chapter+'-'+parsed.fragment
            else:
                file = ROOT / parsed.path
                if not file.is_file(): raise ValueError('Missing standalone resource: '+parsed.path)
                mime = mimetypes.guess_type(file.name)[0] or 'application/octet-stream'
                new = 'data:'+mime+';base64,'+base64.b64encode(file.read_bytes()).decode()
            return attr+'="'+new+'"'
        body = re.sub(r'(src|href)="([^"]+)"', url_rewrite, body)
        sections.append('<section id="'+chapter+'" class="book-chapter">'+body+'</section>')
    css = CSS + '.book-chapter{padding-top:32px;margin-top:44px;border-top:2px solid #dce5ed}.book-chapter>h2:first-child{font-size:29px;margin-top:0}section{scroll-margin-top:20px}@media(max-width:850px){.book-chapter>h2:first-child{font-size:26px}}'
    doc = '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>webuddy 维护人员手册 完整阅读版</title><style>'+css+'</style></head><body><a class="skip" href="#content">跳到正文</a><header><a href="#content">webuddy 维护人员手册</a><small>完整离线阅读版 · 2026.09</small></header><nav aria-label="章节导航"><div class="nav-title">完整阅读版</div>'+nav_html+'</nav><details class="mobile-nav"><summary>打开 19 章目录</summary>'+nav_html+'</details><main id="content"><h1>webuddy 维护人员手册</h1><div class="notice">所有说明图已嵌入，无需解压或访问外网。代码、可编辑图源与工具另见完整交付包。本书不代表已部署或发布到 GitBook。</div>'+''.join(sections)+'</main></body></html>'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(doc)
    print('Built standalone reading edition:', output)


def diagrams():
    for dot in sorted((ROOT / 'diagrams').glob('*.dot')):
        for ext in ('svg', 'png'):
            subprocess.run(['dot', '-Gdpi=150', '-T'+ext, str(dot), '-o', str(ROOT/'assets'/(dot.stem+'.'+ext))], check=True)
    print('Rendered', len(list((ROOT / 'diagrams').glob('*.dot'))), 'editable diagrams')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('check', 'build', 'diagrams', 'single'))
    parser.add_argument('--output')
    args = parser.parse_args()
    if args.command == 'check': check()
    elif args.command == 'build': build(args.output)
    elif args.command == 'single': single(args.output or ROOT / '_preview' / 'reading.html')
    else: diagrams()


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

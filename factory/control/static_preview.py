"""Rehydrate local styles/images from an immutable ZIP for a script-free iframe."""
import base64
import html
import posixpath
import re
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

IMAGE_TYPES = {'.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.gif':'image/gif','.webp':'image/webp','.svg':'image/svg+xml','.ico':'image/x-icon'}


def static_preview(archive, entry):
    names=set(archive.namelist());cache={};budget=0
    def resolve(raw, owner):
        url=urlsplit(raw)
        if url.scheme or url.netloc or not url.path: return None
        target=unquote(url.path)
        root=entry.split('/')[0] if '/' in entry else ''
        candidate=posixpath.normpath(posixpath.join(root if target.startswith('/') else posixpath.dirname(owner), target.lstrip('/')))
        return candidate if candidate in names and not candidate.startswith('../') else None
    def read(name):
        nonlocal budget
        if name not in cache:
            size=archive.getinfo(name).file_size
            if size>1024*1024 or budget+size>3*1024*1024: return None
            budget+=size;cache[name]=archive.read(name)
        return cache[name]
    def image(raw, owner):
        name=resolve(raw,owner)
        if not name: return raw if raw.startswith('data:image/') else ''
        mime=IMAGE_TYPES.get(PurePosixPath(name).suffix.lower());data=read(name) if mime else None
        return f'data:{mime};base64,'+base64.b64encode(data).decode() if data is not None else ''
    def css(text, owner):
        # External imports remain disabled; bundle-produced local CSS is inlined.
        text=re.sub(r'@import\s+[^;]+;', '', text, flags=re.I)
        return re.sub(r'url\(\s*([\'"]?)(.*?)\1\s*\)',lambda m: 'url("'+image(m[2],owner)+'")',text,flags=re.I)
    class Document(HTMLParser):
        def __init__(self):super().__init__(convert_charrefs=False);self.parts=[]
        def handle_starttag(self,tag,attrs):
            values=dict(attrs)
            if tag=='base':return
            if tag=='link' and values.get('rel','').lower()=='stylesheet':
                name=resolve(values.get('href',''),entry)
                data=read(name) if name and name.endswith('.css') else None
                if data is not None:self.parts.append('<style>'+css(data.decode('utf-8','replace'),name)+'</style>')
                return
            updated=[]
            for key,value in attrs:
                if tag=='img' and key=='src':value=image(value or '',entry)
                if key=='srcset':continue
                if key=='style':value=css(value or '',entry)
                updated.append(key if value is None else key+'="'+html.escape(value,quote=True)+'"')
            self.parts.append('<'+tag+(' '+' '.join(updated) if updated else '')+'>')
        def handle_startendtag(self,tag,attrs):self.handle_starttag(tag,attrs)
        def handle_endtag(self,tag):self.parts.append('</'+tag+'>')
        def handle_data(self,data):self.parts.append(data)
        def handle_entityref(self,name):self.parts.append('&'+name+';')
        def handle_charref(self,name):self.parts.append('&#'+name+';')
        def handle_decl(self,decl):self.parts.append('<!'+decl+'>')
    document=Document();document.feed(archive.read(entry).decode('utf-8','replace'))
    result=''.join(document.parts)
    if len(result.encode())>4*1024*1024:raise ValueError('页面预览超过大小限制，请下载后运行')
    return result

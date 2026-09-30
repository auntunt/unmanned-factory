import base64
from html.parser import HTMLParser
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('handbook', Path(__file__).with_name('handbook.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class Links(HTMLParser):
    def __init__(self):
        super().__init__(); self.ids=[]; self.hrefs=[]; self.sources=[];self.scripts=0
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if 'id' in attrs: self.ids.append(attrs['id'])
        if 'href' in attrs: self.hrefs.append(attrs['href'])
        if 'src' in attrs: self.sources.append(attrs['src'])
        if tag=='script': self.scripts+=1


class HandbookTests(unittest.TestCase):
    def test_links_and_manifest(self):
        m.check()

    def test_escapes_html(self):
        value,_=m.render('<script>alert(1)</script>')
        self.assertNotIn('<script>',value)
        self.assertIn('&lt;script&gt;',value)

    def test_code_is_not_linkified(self):
        value=m.inline('`[unsafe](javascript:alert)`')
        self.assertIn('<code>',value);self.assertNotIn('<a ',value)

    def test_blocked_link_scheme(self):
        with self.assertRaises(ValueError): m.inline('[x](javascript:alert)')

    def test_tables_and_alt(self):
        value,_=m.render('| A | B |\n| --- | --- |\n| X | Y |\n\n![图](assets/test.png)')
        self.assertIn('scope="col"',value)
        self.assertIn('alt="图"',value)

    def test_standalone_has_no_missing_anchors_or_external_assets(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'reading.html';m.single(p)
            parsed=Links();parsed.feed(p.read_text())
            self.assertEqual(len(parsed.ids),len(set(parsed.ids)))
            self.assertTrue(all(h[1:] in parsed.ids for h in parsed.hrefs if h.startswith('#')))
            self.assertEqual(parsed.scripts,0)
            self.assertEqual(len(parsed.sources),24)
            for src in parsed.sources:
                self.assertTrue(src.startswith('data:image/png;base64,'))
                self.assertTrue(base64.b64decode(src.split(',',1)[1]).startswith(b'\x89PNG'))
            self.assertTrue(all(h.startswith(('http:','https:','#','data:')) for h in parsed.hrefs))


if __name__=='__main__': unittest.main()

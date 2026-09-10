import io
import zipfile
from factory.control.static_preview import static_preview


def test_archive_relative_and_root_assets_are_embedded_without_network():
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as z:
        z.writestr('dist/index.html','<html><head><link rel="stylesheet" href="/assets/style.css"></head><body><h1>Hello</h1><img src="./assets/icon.png"><script src="/assets/app.js"></script></body></html>')
        z.writestr('dist/assets/style.css','h1{color:red;background:url(icon.png)} @import "https://example.com/x.css";')
        z.writestr('dist/assets/icon.png',b'png')
    with zipfile.ZipFile(stream) as z:result=static_preview(z,'dist/index.html')
    assert '<style>h1{color:red;' in result
    assert result.count('data:image/png;base64,')==2
    assert 'https://example.com' not in result
    assert '<script' in result # Frontend sandbox and CSP keep all script execution disabled.


def test_external_and_traversing_images_are_not_resolved():
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as z:z.writestr('index.html','<img src="https://example.com/x"><img src="../../secret.png">')
    with zipfile.ZipFile(stream) as z:result=static_preview(z,'index.html')
    assert 'https://' not in result and 'secret.png' not in result

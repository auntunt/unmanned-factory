"""让 tests 成为一个真包。

不是风格问题，是能不能跑的问题：test_scope.py 和 test_dispatcher_four.py
会 `from tests.test_dispatcher import ...` 复用夹具。没有这个文件时 tests/
只是命名空间包，而命名空间包在整条 sys.path 扫完都没找到真包时才生效 ——
site-packages 里恰好有个别的库带来的 tests/__init__.py，于是它赢了导入，
本地测试报 ModuleNotFoundError: No module named 'tests.test_dispatcher'。

装了哪些第三方库不该决定这个仓的测试能不能收集，所以在这儿钉死。
"""

"""Frozen pre-split route signatures/decorators and byte-identical middleware."""
import ast
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def dump(node):
    try:
        return ast.dump(node, show_empty=True)
    except TypeError:  # Python <= 3.12 already includes empty fields.
        return ast.dump(node)


def test_route_parameters_and_decorators_unchanged():
    baseline=json.loads((ROOT/'tests/fixtures/app-route-contract.json').read_text())
    rows=[]
    for name in ['app','team_routes','auth_routes','run_routes','webhook_routes']:
        for n in ast.walk(ast.parse((ROOT/f'factory/control/{name}.py').read_text())):
            if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)):
                for d in n.decorator_list:
                    if isinstance(d,ast.Call) and isinstance(d.func,ast.Attribute) and d.func.attr in ('get','post','put','patch','delete','api_route'):
                        rows.append({'name':n.name,'method':d.func.attr,'args':dump(n.args),'decorator_args':[dump(a) for a in d.args],'decorator_keywords':[dump(k) for k in d.keywords],'async':isinstance(n,ast.AsyncFunctionDef)})
    assert sorted(rows,key=lambda r:json.dumps(r,sort_keys=True))==sorted(baseline['routes'],key=lambda r:json.dumps(r,sort_keys=True))


def test_middleware_remains_byte_identical_in_app():
    baseline=json.loads((ROOT/'tests/fixtures/app-route-contract.json').read_text())
    source=(ROOT/'factory/control/app.py').read_text()
    n=next(n for n in ast.walk(ast.parse(source)) if isinstance(n,ast.AsyncFunctionDef) and n.name=='boundary')
    block='\n'.join(source.splitlines()[n.decorator_list[0].lineno-1:n.end_lineno])
    assert hashlib.sha256(block.encode()).hexdigest()==baseline['middleware_sha256']

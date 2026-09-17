"""MFD → 江苏标准 XML 职能包：真实执行的回归。

这些用例跑的是真程序：合成样本按已确认词素编码，转换在隔离运行时里真实执行，
输出逐字节比对。没有一条断言是「某个 mock 被调用过」。

真实工程样本（2.mfd 及甲方参考 XML）不在仓库里，也不在这些用例里——对它的核对结论
写在 docs/mfd/mfd-xml-extraction-report.md。
"""
import json
import struct
import sys
from pathlib import Path

import pytest

from factory.control.capability_packs import validate_manifest
from factory.control.pack_runtime import evaluate, run_tool

PACK = Path(__file__).resolve().parents[1] / 'factory/control/builtin_packs/tools/mfd-xml'
sys.path.insert(0, str(PACK / 'tool'))
sys.path.insert(0, str(PACK / 'fixtures'))


@pytest.fixture(scope='module')
def pack():
    files = {p.relative_to(PACK).as_posix(): p.read_bytes()
             for p in PACK.rglob('*') if p.is_file() and '__pycache__' not in p.parts}
    manifest = validate_manifest(files['webuddy-pack.json'], set(files))
    return manifest, files


def run(pack, name, content):
    manifest, files = pack
    outcome = run_tool({'manifest': manifest}, files, [{'name': name, 'content': content}])
    outputs = {item['path']: item['content'] for item in outcome['outputs']}
    return outcome, outputs


def test_tool_contract_is_complete_and_forbids_network(pack):
    manifest, _ = pack
    assert manifest['tool']['permissions']['network'] is False
    assert manifest['evaluation_policy']['test_set'] == 'fixtures/tests.json'
    # 适用范围必须同时写明未验证的部分，不能只列会通过的格式。
    statuses = {row['status'] for row in manifest['support_matrix']}
    assert 'unsupported' in statuses and 'supported' in statuses
    assert any('整体分帧' in row['format'] for row in manifest['support_matrix'])


def test_real_evaluation_of_the_shipped_test_set_passes(pack):
    manifest, files = pack
    report = evaluate({'manifest': manifest}, files)
    assert report['passed'], report
    assert len(report['cases']) == 3 and all(case['passed'] for case in report['cases'])


def test_synthetic_sample_converts_byte_for_byte(pack):
    _, files = pack
    outcome, outputs = run(pack, 'synthetic_complete.mfd', files['fixtures/synthetic_complete.mfd'])
    assert outcome['status'] == 'succeeded', outcome
    assert outputs['project.xml'] == files['fixtures/expected_complete.xml']
    assert outcome['result'] == {'records_total': 2, 'records_emitted': 2,
                                 'records_unresolved': 0, 'complete': True}


def test_missing_required_fields_never_get_invented(pack):
    _, files = pack
    outcome, outputs = run(pack, 'synthetic_partial.mfd', files['fixtures/synthetic_partial.mfd'])
    assert outcome['status'] == 'failed' and outcome['error_code'] == 'incomplete_extraction'
    xml = outputs['project.xml'].decode()
    # 缺字段的那条记录不能进 XML，更不能被补上 单位/工程量 的默认值。
    assert '011707002001' not in xml and '010101002001' in xml
    assert '单位=""' not in xml and '工程量="0"' not in xml
    report = json.loads(outputs['extraction_report.json'].decode())
    missing = {row['编号']: row['未解析字段'] for row in report['records']}
    assert missing['011707002001'] == ['单位', '工程量', '单价', '合价']
    assert missing['010101002001'] == []
    assert report['records'][1]['offset'].startswith('0x')  # 偏移保留，便于继续逆向


def test_candidate_output_is_kept_and_labelled_rather_than_discarded(pack):
    outcome, outputs = run(pack, 'synthetic_partial.mfd', (PACK / 'fixtures/synthetic_partial.mfd').read_bytes())
    # 文件生成了但校验没过：仍然是 failed，候选文件仍然可下载。
    assert outcome['status'] == 'failed'
    assert set(outputs) == {'project.xml', 'extraction_report.json'}
    assert outcome['validation_status'] != 'passed'


def test_non_mfd_and_truncated_input_are_refused_without_guessing(pack):
    outcome, _ = run(pack, 'not_mfd.bin', (PACK / 'fixtures/not_mfd.bin').read_bytes())
    assert outcome['error_code'] == 'not_mfd'
    truncated, _ = run(pack, 'short.mfd', b'^TF' + b'\x00' * 32)
    assert truncated['error_code'] == 'not_mfd'
    unknown_tag, _ = run(pack, 'odd.mfd', b'^TF\x00' + b'\x00' * (0x400 - 4) + b'\x7f' + b'\x00' * 32)
    assert unknown_tag['error_code'] == 'not_mfd'


def test_no_records_is_reported_instead_of_an_empty_success(pack):
    header = b'^TF\x00' + b'\x00' * (0x400 - 4) + b'\x03' + struct.pack('<H', 0)
    outcome, outputs = run(pack, 'empty.mfd', header + b'\x00' * 64)
    assert outcome['status'] == 'failed' and outcome['error_code'] == 'no_records'
    assert '<清单' not in outputs['project.xml'].decode()


def test_gbk_names_are_decoded_not_dropped():
    """逆向笔记把这个槽位记成 ASCII；实测是 GBK，按 ASCII 过滤会静默丢掉全部中文名称。"""
    from mfd_lex import lex
    body = '挖一般土方'.encode('gbk')
    stream = b'\x02\x2c\x04\x08\x06' + bytes([len(body)]) + body
    tokens, _ = lex(stream, 0)
    assert [t[2] for t in tokens if t[1] == 'str'] == ['挖一般土方']


def test_money_normalisation_is_declared_and_quantity_is_not_touched():
    from xml_writer import render
    xml = render([{'code': '010101002001', 'name': '合成', 'unit': 'm3',
                   'values': {'工程量': 12.345678, '单价': 3949.5699999999997, '合价': 0.005}}])
    assert '工程量="12.345678"' in xml  # 工程量不归一
    assert '单价="3949.57"' in xml and '合价="0.01"' in xml  # 金额 2 位 ROUND_HALF_UP
    assert 'E' not in xml.split('<清单')[1]  # xs:decimal 不接受科学计数法


def test_values_must_follow_a_recognised_unit_or_they_stay_unassigned():
    """没有单位就无法确定后面的 double 属于哪个字段——此时必须留空，而不是按顺序硬塞。"""
    from mfd_records import records
    tokens = [(0, 'str', '010101002001', False), (10, 'str', '某清单', False),
              (20, 'f64', 1.0, False), (30, 'f64', 2.0, False)]
    assert records(tokens)[0]['values'] == {}
    assert records(tokens)[0]['missing'] == ['单位', '工程量', '单价', '合价']


def test_fixtures_are_synthetic_and_regenerable():
    """样本必须能从提交的生成器重放出来，且不含任何客户业务值。"""
    import make_fixtures
    expected = (PACK / 'fixtures/synthetic_complete.mfd').read_bytes()
    rebuilt = make_fixtures.container(2,
        make_fixtures.record(0x2c, '010101002001', '合成土方工程', 'm3', (12.5, 486.2, 6077.5))
        + make_fixtures.record(0x4c, '011707001001', '合成安全文明施工', '项', (1.0, 3949.5699999999997, 3949.57)))
    assert rebuilt == expected
    assert '合成' in (PACK / 'fixtures/expected_complete.xml').read_text(encoding='utf-8')

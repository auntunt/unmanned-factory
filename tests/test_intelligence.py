"""商业情报系统测试"""

import pytest
from factory.intelligence.models import (
    Analysis,
    Company,
    Evidence,
    IndustryDynamic,
    Personnel,
    Publication,
    init_db,
)


@pytest.fixture
def db_session():
    """测试数据库 session"""
    Session = init_db(":memory:")  # 使用内存数据库
    session = Session()
    yield session
    session.close()


def test_create_company(db_session):
    """测试创建公司"""
    company = Company(
        name="测试公司",
        short_name="测试",
        stock_code="000001",
        industry="软件",
        is_listed=True,
    )
    db_session.add(company)
    db_session.commit()

    assert company.id is not None
    assert company.name == "测试公司"


def test_create_personnel(db_session):
    """测试创建人物"""
    company = Company(name="测试公司", stock_code="000001", industry="软件")
    db_session.add(company)
    db_session.commit()

    person = Personnel(
        name="张三",
        company_id=company.id,
        title="CEO",
        is_executive=True,
        expertise=["技术", "管理"],
    )
    db_session.add(person)
    db_session.commit()

    assert person.id is not None
    assert person.company.name == "测试公司"


def test_create_dynamic(db_session):
    """测试创建行业动态"""
    company = Company(name="测试公司", stock_code="000001", industry="软件")
    db_session.add(company)
    db_session.commit()

    dynamic = IndustryDynamic(
        company_id=company.id,
        type="financial",
        title="Q3 财报",
        summary="营收增长 20%",
        published_date="2024-11-01",
        source_url="https://example.com/report",
        source_name="官网",
    )
    db_session.add(dynamic)
    db_session.commit()

    assert dynamic.id is not None
    assert dynamic.type == "financial"


def test_create_evidence(db_session):
    """测试创建证据"""
    company = Company(name="测试公司", stock_code="000001", industry="软件")
    db_session.add(company)
    db_session.commit()

    evidence = Evidence(
        company_id=company.id,
        text="公司营收增长 20%",
        source_url="https://example.com/report",
        source_name="官网",
        category="fact",
        confidence="high",
        is_verified=True,
    )
    db_session.add(evidence)
    db_session.commit()

    assert evidence.id is not None
    assert evidence.category == "fact"


def test_create_analysis(db_session):
    """测试创建分析"""
    company = Company(name="测试公司", stock_code="000001", industry="软件")
    db_session.add(company)
    db_session.commit()

    evidence = Evidence(
        company_id=company.id,
        text="市场需求旺盛",
        source_url="https://example.com",
        source_name="新闻",
        category="opportunity",
        confidence="high",
    )
    db_session.add(evidence)
    db_session.commit()

    analysis = Analysis(
        company_id=company.id,
        title="增长机会分析",
        summary="基于市场需求旺盛的证据，公司存在显著增长机会。",
        opportunity_type="sales",
        evidence_ids=[evidence.id],
        generated_by="claude-opus-4",
        is_ai_generated=True,
    )
    db_session.add(analysis)
    db_session.commit()

    assert analysis.id is not None
    assert analysis.is_ai_generated is True
    assert evidence.id in analysis.evidence_ids


def test_company_relationships(db_session):
    """测试公司关联关系"""
    company = Company(name="测试公司", stock_code="000001", industry="软件")
    db_session.add(company)
    db_session.commit()

    # 添加人物
    person = Personnel(
        name="张三", company_id=company.id, title="CEO", is_executive=True
    )
    db_session.add(person)

    # 添加动态
    dynamic = IndustryDynamic(
        company_id=company.id,
        type="news",
        title="新闻",
        summary="摘要",
        published_date="2024-01-01",
        source_url="https://example.com",
        source_name="新闻网",
    )
    db_session.add(dynamic)

    # 添加证据
    evidence = Evidence(
        company_id=company.id,
        text="证据文本",
        source_url="https://example.com",
        source_name="来源",
        category="fact",
        confidence="high",
    )
    db_session.add(evidence)
    db_session.commit()

    # 验证关联
    assert len(company.personnel) == 1
    assert len(company.dynamics) == 1
    assert len(company.evidence) == 1
    assert company.personnel[0].name == "张三"


def test_publication(db_session):
    """测试公开发表内容"""
    company = Company(name="测试公司", stock_code="000001", industry="软件")
    db_session.add(company)
    db_session.commit()

    person = Personnel(
        name="张三", company_id=company.id, title="CEO", is_executive=True
    )
    db_session.add(person)
    db_session.commit()

    pub = Publication(
        author_id=person.id,
        title="技术演讲",
        content_type="speech",
        content_summary="介绍了新技术",
        published_date="2024-10-01",
        source_url="https://example.com/speech",
        source_name="技术大会",
        topics=["技术", "创新"],
    )
    db_session.add(pub)
    db_session.commit()

    assert pub.id is not None
    assert pub.author.name == "张三"
    assert "技术" in pub.topics


def test_query_by_company(db_session):
    """测试按公司查询"""
    company1 = Company(name="公司A", stock_code="000001", industry="软件")
    company2 = Company(name="公司B", stock_code="000002", industry="硬件")
    db_session.add_all([company1, company2])
    db_session.commit()

    # 为 company1 添加数据
    person1 = Personnel(name="张三", company_id=company1.id, title="CEO")
    dynamic1 = IndustryDynamic(
        company_id=company1.id,
        type="news",
        title="新闻A",
        summary="摘要",
        published_date="2024-01-01",
        source_url="https://example.com",
        source_name="新闻网",
    )
    db_session.add_all([person1, dynamic1])

    # 为 company2 添加数据
    person2 = Personnel(name="李四", company_id=company2.id, title="CTO")
    db_session.add(person2)
    db_session.commit()

    # 查询
    company1_personnel = (
        db_session.query(Personnel).filter(Personnel.company_id == company1.id).all()
    )
    assert len(company1_personnel) == 1
    assert company1_personnel[0].name == "张三"

    company2_personnel = (
        db_session.query(Personnel).filter(Personnel.company_id == company2.id).all()
    )
    assert len(company2_personnel) == 1
    assert company2_personnel[0].name == "李四"

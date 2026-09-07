"""商业情报数据模型

所有表都支持中文，使用 UTF-8 编码。每条信息都可追溯来源。
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker


class Base(DeclarativeBase):
    """所有模型的基类"""


class Company(Base):
    """公司档案

    支持上市公司和非上市公司。上市公司会有更多公开信息。
    """

    __tablename__ = "companies"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False, index=True)  # 公司全称
    name_en = Column(String(200))  # 英文名称
    short_name = Column(String(100))  # 简称
    stock_code = Column(String(20), index=True)  # 股票代码（如 "000001.SZ"）
    stock_exchange = Column(String(20))  # 交易所（SSE/SZSE）
    industry = Column(String(100))  # 所属行业
    business_scope = Column(Text)  # 业务范围
    founded_date = Column(String(20))  # 成立日期
    headquarters = Column(String(200))  # 总部地址
    website = Column(String(200))  # 官方网站
    description = Column(Text)  # 公司简介
    is_listed = Column(Boolean, default=False)  # 是否上市
    market_cap = Column(Float)  # 市值（亿元）
    employee_count = Column(Integer)  # 员工数

    # 业务线结构，JSON 格式: [{"name": "云服务", "description": "...", "revenue_pct": 45}]
    business_lines = Column(JSON)

    # 组织架构，JSON 格式: [{"dept": "技术中心", "head": "张三", "level": 1}]
    org_structure = Column(JSON)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 关系
    personnel = relationship("Personnel", back_populates="company")
    dynamics = relationship("IndustryDynamic", back_populates="company")
    evidence = relationship("Evidence", back_populates="company")


class Personnel(Base):
    """关键人物档案

    只收集公开的职业信息，不包含私人敏感信息。
    """

    __tablename__ = "personnel"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, index=True)
    name_en = Column(String(100))  # 英文名
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    title = Column(String(200))  # 职位
    department = Column(String(200))  # 部门
    is_executive = Column(Boolean, default=False)  # 是否高管

    # 职业履历，JSON 格式: [{"company": "...", "title": "...", "from": "2018", "to": "2020"}]
    career_history = Column(JSON)

    # 教育背景，JSON 格式: [{"school": "...", "degree": "...", "year": "2010"}]
    education = Column(JSON)

    # 专业领域
    expertise = Column(JSON)  # ["云计算", "AI"]

    # 公开发表内容摘要
    bio = Column(Text)  # 个人简介

    # 公开社交媒体（仅职业平台）
    linkedin_url = Column(String(200))
    weibo_url = Column(String(200))

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 关系
    company = relationship("Company", back_populates="personnel")
    publications = relationship("Publication", back_populates="author")


class Publication(Base):
    """公开发表内容

    包括采访、演讲、文章、公开观点等。
    """

    __tablename__ = "publications"

    id = Column(Integer, primary_key=True)
    author_id = Column(Integer, ForeignKey("personnel.id"), nullable=False)
    title = Column(String(500), nullable=False)
    content_type = Column(String(50))  # interview/speech/article/post
    content_summary = Column(Text)  # 内容摘要
    published_date = Column(String(20))  # 发表日期
    source_url = Column(String(500))  # 来源链接
    source_name = Column(String(200))  # 来源名称

    # 关键主题
    topics = Column(JSON)  # ["数字化转型", "行业趋势"]

    created_at = Column(DateTime, default=datetime.utcnow)

    # 关系
    author = relationship("Personnel", back_populates="publications")


class IndustryDynamic(Base):
    """行业动态

    包括公告、财报、新闻、招聘、监管政策等。
    """

    __tablename__ = "industry_dynamics"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), index=True)

    # 动态类型
    type = Column(String(50), nullable=False, index=True)  # announcement/financial/news/hiring/policy

    title = Column(String(500), nullable=False)
    summary = Column(Text)
    content = Column(Text)  # 完整内容

    published_date = Column(String(20), index=True)  # 发布日期

    # 来源信息
    source_url = Column(String(500), nullable=False)  # 来源链接
    source_name = Column(String(200))  # 来源名称
    source_type = Column(String(50))  # official/media/regulatory

    # 标签和分类
    tags = Column(JSON)  # ["财报", "Q3", "业绩"]

    # 财报专用字段
    financial_period = Column(String(20))  # 如 "2024Q3"
    revenue = Column(Float)  # 营收（亿元）
    profit = Column(Float)  # 利润（亿元）

    created_at = Column(DateTime, default=datetime.utcnow)

    # 关系
    company = relationship("Company", back_populates="dynamics")
    evidence = relationship("Evidence", back_populates="dynamic")


class Evidence(Base):
    """证据片段

    从各种来源提取的可引用证据，支持分析和溯源。
    """

    __tablename__ = "evidence"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), index=True)
    dynamic_id = Column(Integer, ForeignKey("industry_dynamics.id"))

    # 证据内容
    text = Column(Text, nullable=False)  # 原文片段
    context = Column(Text)  # 上下文

    # 来源
    source_url = Column(String(500), nullable=False)
    source_name = Column(String(200))
    extracted_date = Column(String(20))  # 提取日期

    # 分类
    category = Column(String(100))  # opportunity/risk/trend/fact
    topics = Column(JSON)  # 关联主题

    # 置信度和验证状态
    confidence = Column(String(20))  # high/medium/low
    is_verified = Column(Boolean, default=False)

    created_at = Column(DateTime, default=datetime.utcnow)

    # 关系
    company = relationship("Company", back_populates="evidence")
    dynamic = relationship("IndustryDynamic", back_populates="evidence")


class Analysis(Base):
    """分析摘要

    基于证据的商业机会分析。AI 生成的分析与事实证据明确分离。
    """

    __tablename__ = "analyses"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)

    # 分析内容
    title = Column(String(500), nullable=False)
    summary = Column(Text, nullable=False)  # 分析摘要
    opportunity_type = Column(String(100))  # partnership/sales/investment

    # 证据引用（ID 列表）
    evidence_ids = Column(JSON, nullable=False)  # [1, 5, 12]

    # 分析元数据
    generated_by = Column(String(50))  # AI 模型标识
    generated_at = Column(DateTime, default=datetime.utcnow)
    is_ai_generated = Column(Boolean, default=True)  # 是否 AI 生成

    # 人工审核
    is_reviewed = Column(Boolean, default=False)
    reviewed_by = Column(String(100))
    review_notes = Column(Text)

    created_at = Column(DateTime, default=datetime.utcnow)


# 索引优化
Index("idx_personnel_company", Personnel.company_id)
Index("idx_dynamics_company_type", IndustryDynamic.company_id, IndustryDynamic.type)
Index("idx_dynamics_date", IndustryDynamic.published_date.desc())
Index("idx_evidence_company", Evidence.company_id)


def init_db(db_path: str = "intelligence.db") -> sessionmaker:
    """初始化数据库并返回 session maker"""
    engine = create_engine(f"sqlite:///{db_path}", echo=False)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)

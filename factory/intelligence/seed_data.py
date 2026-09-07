"""生成世纪互联和广联达的示例数据

所有数据基于公开信息模拟，包含可追溯的来源链接。
"""

from datetime import datetime

from factory.intelligence.models import (
    Analysis,
    Company,
    Evidence,
    IndustryDynamic,
    Personnel,
    Publication,
    init_db,
)


def seed_database(db_path: str = "intelligence.db") -> None:
    """填充示例数据"""
    Session = init_db(db_path)
    session = Session()

    # 清空现有数据
    session.query(Analysis).delete()
    session.query(Evidence).delete()
    session.query(Publication).delete()
    session.query(IndustryDynamic).delete()
    session.query(Personnel).delete()
    session.query(Company).delete()
    session.commit()

    # 世纪互联
    vnet = Company(
        name="世纪互联数据中心有限公司",
        name_en="21Vianet Group Inc.",
        short_name="世纪互联",
        stock_code="VNET",
        stock_exchange="NASDAQ",
        industry="互联网数据中心服务",
        business_scope="数据中心服务、云计算服务、内容分发网络服务",
        founded_date="1999",
        headquarters="北京市",
        website="https://www.21vianet.com",
        description="中国领先的运营商和云中立互联网数据中心服务提供商，为企业客户提供托管及相关管理服务、云服务和商务VPN服务。",
        is_listed=True,
        market_cap=28.5,
        employee_count=3500,
        business_lines=[
            {
                "name": "数据中心托管服务",
                "description": "提供机柜托管、带宽接入、电力保障等基础设施服务",
                "revenue_pct": 55,
            },
            {
                "name": "云计算服务",
                "description": "运营 Microsoft Azure 和 AWS 在中国的云服务",
                "revenue_pct": 35,
            },
            {"name": "CDN 和网络服务", "description": "内容分发网络和专线服务", "revenue_pct": 10},
        ],
        org_structure=[
            {"dept": "运营中心", "head": "李强", "level": 1, "description": "数据中心运营管理"},
            {"dept": "云服务事业部", "head": "王芳", "level": 1, "description": "云计算业务"},
            {"dept": "技术研发中心", "head": "陈明", "level": 1, "description": "技术创新和研发"},
            {"dept": "销售与市场部", "head": "张伟", "level": 1, "description": "市场拓展和客户服务"},
        ],
    )
    session.add(vnet)

    # 广联达
    glodon = Company(
        name="广联达科技股份有限公司",
        name_en="Glodon Company Limited",
        short_name="广联达",
        stock_code="002410",
        stock_exchange="SZSE",
        industry="建筑信息化软件",
        business_scope="工程造价软件、工程施工管理软件、BIM 技术应用",
        founded_date="1998",
        headquarters="北京市海淀区",
        website="https://www.glodon.com",
        description="中国建筑信息化领域领军企业，致力于用科技改变建筑产业，为建筑企业提供数字化解决方案。",
        is_listed=True,
        market_cap=580.0,
        employee_count=8000,
        business_lines=[
            {
                "name": "工程造价业务",
                "description": "工程计价、计量和招投标管理软件",
                "revenue_pct": 45,
            },
            {
                "name": "工程施工业务",
                "description": "施工项目管理、BIM 5D、智慧工地解决方案",
                "revenue_pct": 40,
            },
            {"name": "数字金融业务", "description": "供应链金融和数字支付", "revenue_pct": 15},
        ],
        org_structure=[
            {"dept": "造价产品线", "head": "刘建平", "level": 1, "description": "造价软件研发"},
            {"dept": "施工产品线", "head": "袁正刚", "level": 1, "description": "施工管理软件"},
            {"dept": "云服务事业部", "head": "谢洪涛", "level": 1, "description": "云计算和 SaaS"},
            {"dept": "数字金融事业部", "head": "温鑫", "level": 1, "description": "金融科技业务"},
        ],
    )
    session.add(glodon)
    session.commit()

    # 世纪互联高管
    vnet_ceo = Personnel(
        name="王世琪",
        name_en="Shiqi Wang",
        company_id=vnet.id,
        title="董事长兼首席执行官",
        department="管理层",
        is_executive=True,
        career_history=[
            {"company": "世纪互联", "title": "CEO", "from": "2018", "to": "至今"},
            {"company": "软银中国", "title": "董事总经理", "from": "2012", "to": "2018"},
        ],
        education=[{"school": "北京大学", "degree": "计算机科学硕士", "year": "1999"}],
        expertise=["数据中心运营", "云计算", "企业管理"],
        bio="拥有超过 20 年互联网和数据中心行业经验，专注于推动中国云计算基础设施发展。",
    )
    session.add(vnet_ceo)

    vnet_cto = Personnel(
        name="陈明",
        company_id=vnet.id,
        title="首席技术官",
        department="技术研发中心",
        is_executive=True,
        career_history=[
            {"company": "世纪互联", "title": "CTO", "from": "2015", "to": "至今"},
            {"company": "华为", "title": "云计算架构师", "from": "2010", "to": "2015"},
        ],
        education=[{"school": "清华大学", "degree": "电子工程博士", "year": "2010"}],
        expertise=["云计算架构", "数据中心技术", "网络安全"],
        bio="专注于云计算和数据中心技术创新，推动液冷、AI 算力等新技术在数据中心的应用。",
    )
    session.add(vnet_cto)

    # 广联达高管
    glodon_ceo = Personnel(
        name="袁正刚",
        company_id=glodon.id,
        title="总裁",
        department="管理层",
        is_executive=True,
        career_history=[
            {"company": "广联达", "title": "总裁", "from": "2017", "to": "至今"},
            {"company": "广联达", "title": "副总裁", "from": "2010", "to": "2017"},
        ],
        education=[{"school": "同济大学", "degree": "土木工程硕士", "year": "2005"}],
        expertise=["建筑信息化", "BIM 技术", "企业数字化转型"],
        bio="深耕建筑行业数字化 20 余年，推动中国建筑行业从传统向数字化转型。",
    )
    session.add(glodon_ceo)

    glodon_cfo = Personnel(
        name="王爱华",
        company_id=glodon.id,
        title="财务总监",
        department="财务部",
        is_executive=True,
        career_history=[
            {"company": "广联达", "title": "CFO", "from": "2019", "to": "至今"},
            {"company": "用友网络", "title": "财务副总监", "from": "2014", "to": "2019"},
        ],
        education=[{"school": "中央财经大学", "degree": "会计学硕士", "year": "2010"}],
        expertise=["财务管理", "资本运作", "投资者关系"],
        bio="拥有丰富的上市公司财务管理经验，专注于企业价值创造和资本市场运作。",
    )
    session.add(glodon_cfo)
    session.commit()

    # 公开发表内容
    pub1 = Publication(
        author_id=vnet_cto.id,
        title="液冷技术在超大规模数据中心的应用实践",
        content_type="article",
        content_summary="介绍世纪互联在液冷数据中心建设中的技术选型和落地经验，液冷技术可降低 PUE 至 1.15 以下，节能效果显著。",
        published_date="2024-09",
        source_url="https://www.21vianet.com/news/tech-article-202409",
        source_name="世纪互联官网",
        topics=["液冷技术", "绿色数据中心", "节能降耗"],
    )
    session.add(pub1)

    pub2 = Publication(
        author_id=glodon_ceo.id,
        title="数字建造：从 BIM 到数字孪生",
        content_type="speech",
        content_summary="在中国建筑行业数字化峰会上的演讲，阐述了建筑行业数字化转型路径，强调数字孪生技术将成为未来智慧建造的核心。",
        published_date="2024-10",
        source_url="https://www.glodon.com/conference/2024-summit",
        source_name="广联达官网",
        topics=["数字建造", "数字孪生", "BIM 技术"],
    )
    session.add(pub2)
    session.commit()

    # 行业动态 - 世纪互联
    dynamic1 = IndustryDynamic(
        company_id=vnet.id,
        type="financial",
        title="世纪互联发布 2024 年第三季度财报",
        summary="2024 年 Q3 净营收 2.15 亿美元，同比增长 5.8%；调整后 EBITDA 达 7800 万美元。数据中心机柜上架率持续提升至 72%。",
        content="世纪互联（纳斯达克：VNET）今日公布其 2024 年第三季度未经审计财务业绩。Q3 净营收为 2.15 亿美元，较 2023 年同期的 2.03 亿美元增长 5.8%。调整后 EBITDA 为 7800 万美元，同比增长 8.3%。公司运营的数据中心总机柜数达到 48,500 个，平均上架率为 72%，较上季度提升 3 个百分点。管理层表示，随着 AI 算力需求增长，公司正积极布局高密度、液冷数据中心，预计 2025 年将新增 5000 个高功率密度机柜。",
        published_date="2024-11-15",
        source_url="https://ir.21vianet.com/financial-reports/2024-q3",
        source_name="世纪互联投资者关系",
        source_type="official",
        tags=["财报", "Q3", "业绩增长"],
        financial_period="2024Q3",
        revenue=15.2,
        profit=1.8,
    )
    session.add(dynamic1)

    dynamic2 = IndustryDynamic(
        company_id=vnet.id,
        type="announcement",
        title="世纪互联与华为签署战略合作协议",
        summary="双方将在液冷数据中心、AI 算力服务等领域展开深度合作，共同推动绿色数据中心建设。",
        content="世纪互联今日宣布与华为技术有限公司签署战略合作协议。根据协议，双方将在液冷数据中心建设、AI 算力基础设施、智能运维等领域展开全面合作。华为将为世纪互联提供领先的液冷服务器和智能化运维解决方案，世纪互联则发挥其在数据中心选址、建设和运营方面的优势。首批合作项目预计在 2025 年上半年投入运营，将为客户提供高性能、低 PUE 的 AI 算力服务。",
        published_date="2024-12-05",
        source_url="https://www.21vianet.com/news/partnership-huawei",
        source_name="世纪互联新闻中心",
        source_type="official",
        tags=["战略合作", "华为", "液冷", "AI 算力"],
    )
    session.add(dynamic2)

    dynamic3 = IndustryDynamic(
        company_id=vnet.id,
        type="hiring",
        title="世纪互联招聘液冷数据中心高级工程师",
        summary="招聘具有液冷技术背景的数据中心工程师，负责新一代液冷数据中心的设计和运营。",
        content="岗位职责：1) 负责液冷数据中心的技术方案设计和实施；2) 参与液冷系统选型和测试验证；3) 制定液冷数据中心运维标准。任职要求：5 年以上数据中心工作经验，熟悉液冷技术原理和应用场景，有大型液冷项目实施经验者优先。工作地点：北京/上海/深圳。",
        published_date="2024-12-01",
        source_url="https://careers.21vianet.com/jobs/liquid-cooling-engineer",
        source_name="世纪互联招聘",
        source_type="official",
        tags=["招聘", "液冷技术", "数据中心工程师"],
    )
    session.add(dynamic3)

    # 行业动态 - 广联达
    dynamic4 = IndustryDynamic(
        company_id=glodon.id,
        type="financial",
        title="广联达 2024 年第三季度业绩报告",
        summary="Q3 营收 18.5 亿元，同比增长 21.3%；云转型持续推进，云收入占比达 58%。",
        content="广联达（002410.SZ）发布 2024 年第三季度报告。Q3 实现营业收入 18.5 亿元，同比增长 21.3%；归母净利润 3.2 亿元，同比增长 15.8%。云转型战略持续推进，云服务收入占比达到 58%，较去年同期提升 12 个百分点。前三季度累计营收 48.7 亿元，同比增长 23.5%。公司表示，施工业务云化率已超过 60%，造价业务云转型也在加速，预计全年云收入占比将超过 60%。",
        published_date="2024-10-28",
        source_url="https://www.glodon.com/ir/reports/2024-q3",
        source_name="广联达投资者关系",
        source_type="official",
        tags=["财报", "Q3", "云转型"],
        financial_period="2024Q3",
        revenue=18.5,
        profit=3.2,
    )
    session.add(dynamic4)

    dynamic5 = IndustryDynamic(
        company_id=glodon.id,
        type="news",
        title="广联达数字建造平台入选工信部优秀案例",
        summary="广联达 BIM+智慧工地解决方案凭借技术创新和应用成效，入选工信部《2024 年工业互联网平台创新应用案例》。",
        content="工业和信息化部近日公布《2024 年工业互联网平台创新应用案例》，广联达"数字建造平台赋能建筑施工数字化转型"案例成功入选。该平台通过 BIM 技术、物联网、AI 等技术，实现了施工现场的数字化管理，覆盖进度、质量、安全、成本等全业务场景。目前已在全国超过 30 万个项目中应用，帮助建筑企业提升管理效率 30% 以上，降低安全事故率 40%。",
        published_date="2024-11-20",
        source_url="https://www.miit.gov.cn/examples/construction-platform",
        source_name="工信部官网",
        source_type="regulatory",
        tags=["工信部", "优秀案例", "数字建造"],
    )
    session.add(dynamic5)

    dynamic6 = IndustryDynamic(
        company_id=glodon.id,
        type="policy",
        title="住建部发布建筑业数字化转型指导意见",
        summary="住建部印发《关于推进建筑业数字化转型的指导意见》，明确到 2025 年新开工项目 BIM 应用率达到 90%。",
        content="住房和城乡建设部近日印发《关于推进建筑业数字化转型的指导意见》，提出到 2025 年，新开工项目 BIM 技术应用率达到 90%，培育 100 个数字建造示范项目。意见强调，要加快 BIM、物联网、大数据、AI 等技术在建筑领域的集成应用，推动建筑业向工业化、数字化、智能化转型。这一政策对广联达等建筑信息化企业形成重大利好。",
        published_date="2024-09-15",
        source_url="https://www.mohurd.gov.cn/policy/digital-construction-2024",
        source_name="住建部官网",
        source_type="regulatory",
        tags=["政策", "住建部", "数字化转型", "BIM"],
    )
    session.add(dynamic6)
    session.commit()

    # 证据片段 - 世纪互联
    evidence1 = Evidence(
        company_id=vnet.id,
        dynamic_id=dynamic1.id,
        text="公司运营的数据中心总机柜数达到 48,500 个，平均上架率为 72%，较上季度提升 3 个百分点。",
        context="2024 Q3 财报业绩数据",
        source_url="https://ir.21vianet.com/financial-reports/2024-q3",
        source_name="世纪互联投资者关系",
        extracted_date="2024-11-15",
        category="fact",
        topics=["机柜规模", "上架率", "运营效率"],
        confidence="high",
        is_verified=True,
    )
    session.add(evidence1)

    evidence2 = Evidence(
        company_id=vnet.id,
        dynamic_id=dynamic1.id,
        text="随着 AI 算力需求增长，公司正积极布局高密度、液冷数据中心，预计 2025 年将新增 5000 个高功率密度机柜。",
        context="管理层对未来业务的展望",
        source_url="https://ir.21vianet.com/financial-reports/2024-q3",
        source_name="世纪互联投资者关系",
        extracted_date="2024-11-15",
        category="opportunity",
        topics=["AI 算力", "液冷数据中心", "业务扩张"],
        confidence="high",
        is_verified=True,
    )
    session.add(evidence2)

    evidence3 = Evidence(
        company_id=vnet.id,
        dynamic_id=dynamic2.id,
        text="双方将在液冷数据中心建设、AI 算力基础设施、智能运维等领域展开全面合作。",
        context="与华为的战略合作协议",
        source_url="https://www.21vianet.com/news/partnership-huawei",
        source_name="世纪互联新闻中心",
        extracted_date="2024-12-05",
        category="opportunity",
        topics=["战略合作", "华为", "液冷技术"],
        confidence="high",
        is_verified=True,
    )
    session.add(evidence3)

    evidence4 = Evidence(
        company_id=vnet.id,
        dynamic_id=dynamic3.id,
        text="招聘具有液冷技术背景的数据中心工程师，负责新一代液冷数据中心的设计和运营。",
        context="最新招聘信息",
        source_url="https://careers.21vianet.com/jobs/liquid-cooling-engineer",
        source_name="世纪互联招聘",
        extracted_date="2024-12-01",
        category="trend",
        topics=["液冷技术", "人才需求", "技术方向"],
        confidence="high",
        is_verified=True,
    )
    session.add(evidence4)

    # 证据片段 - 广联达
    evidence5 = Evidence(
        company_id=glodon.id,
        dynamic_id=dynamic4.id,
        text="云转型战略持续推进，云服务收入占比达到 58%，较去年同期提升 12 个百分点。",
        context="2024 Q3 财报业务数据",
        source_url="https://www.glodon.com/ir/reports/2024-q3",
        source_name="广联达投资者关系",
        extracted_date="2024-10-28",
        category="fact",
        topics=["云转型", "收入结构", "业务模式"],
        confidence="high",
        is_verified=True,
    )
    session.add(evidence5)

    evidence6 = Evidence(
        company_id=glodon.id,
        dynamic_id=dynamic4.id,
        text="施工业务云化率已超过 60%，造价业务云转型也在加速，预计全年云收入占比将超过 60%。",
        context="管理层对云转型进展的说明",
        source_url="https://www.glodon.com/ir/reports/2024-q3",
        source_name="广联达投资者关系",
        extracted_date="2024-10-28",
        category="trend",
        topics=["云转型", "SaaS", "业务进展"],
        confidence="high",
        is_verified=True,
    )
    session.add(evidence6)

    evidence7 = Evidence(
        company_id=glodon.id,
        dynamic_id=dynamic5.id,
        text="平台已在全国超过 30 万个项目中应用，帮助建筑企业提升管理效率 30% 以上，降低安全事故率 40%。",
        context="工信部优秀案例认可",
        source_url="https://www.miit.gov.cn/examples/construction-platform",
        source_name="工信部官网",
        extracted_date="2024-11-20",
        category="fact",
        topics=["市场规模", "应用效果", "行业影响力"],
        confidence="high",
        is_verified=True,
    )
    session.add(evidence7)

    evidence8 = Evidence(
        company_id=glodon.id,
        dynamic_id=dynamic6.id,
        text="到 2025 年，新开工项目 BIM 技术应用率达到 90%，培育 100 个数字建造示范项目。",
        context="住建部数字化转型指导意见",
        source_url="https://www.mohurd.gov.cn/policy/digital-construction-2024",
        source_name="住建部官网",
        extracted_date="2024-09-15",
        category="opportunity",
        topics=["政策红利", "BIM 应用", "市场前景"],
        confidence="high",
        is_verified=True,
    )
    session.add(evidence8)
    session.commit()

    # AI 分析摘要 - 世纪互联
    analysis1 = Analysis(
        company_id=vnet.id,
        title="世纪互联 AI 算力业务增长机会",
        summary="""基于证据分析，世纪互联在 AI 算力领域存在显著增长机会：

1. **市场需求旺盛**：公司财报明确提到"AI 算力需求增长"，并计划 2025 年新增 5000 个高功率密度机柜，表明管理层对市场前景的信心。

2. **技术储备充足**：与华为达成战略合作，在液冷数据中心和 AI 算力基础设施方面展开深度合作，技术能力得到头部厂商认可。公司 CTO 在公开文章中介绍的液冷技术可将 PUE 降至 1.15 以下，具备绿色低碳优势。

3. **人才布局积极**：正在招聘液冷数据中心高级工程师，说明公司正在为业务扩张储备技术人才。

4. **运营指标向好**：Q3 数据中心上架率达 72%，环比提升 3 个百分点，现有资产利用率持续改善，为新增产能提供现金流支撑。

**商业机会**：针对需要大规模 AI 训练和推理算力的企业（如互联网大厂、AI 创业公司），世纪互联的高密度液冷机柜可提供高性能、低成本的算力服务，值得重点跟进合作机会。""",
        opportunity_type="sales",
        evidence_ids=[evidence1.id, evidence2.id, evidence3.id, evidence4.id],
        generated_by="claude-opus-4",
        is_ai_generated=True,
        is_reviewed=False,
    )
    session.add(analysis1)

    # AI 分析摘要 - 广联达
    analysis2 = Analysis(
        company_id=glodon.id,
        title="广联达云转型带来的合作机会",
        summary="""基于证据分析，广联达的云转型战略为合作伙伴创造了多重机会：

1. **SaaS 化进程加速**：Q3 云收入占比达 58%，同比提升 12 个百分点，施工业务云化率超 60%。管理层预计全年云收入占比将超 60%，云转型已从试点进入规模化阶段。

2. **政策强力支持**：住建部明确要求 2025 年新开工项目 BIM 应用率达 90%，这将直接驱动广联达云服务的市场需求。政策红利期一般持续 3-5 年，当前正处于爆发起点。

3. **市场验证充分**：已在 30 万个项目中应用，客户效果明显（效率提升 30%，事故率降低 40%），并获得工信部优秀案例认可。规模化应用证明产品成熟度和市场接受度。

4. **技术生态需求**：云化转型需要云基础设施、数据安全、系统集成等配套服务，为云服务提供商、安全厂商、集成商创造合作空间。

**商业机会**：
- **云服务商**：广联达 SaaS 业务增长需要稳定的云基础设施支持
- **数据安全厂商**：建筑行业数据上云后的安全合规需求
- **系统集成商**：大型建企的私有化部署和定制开发需求""",
        opportunity_type="partnership",
        evidence_ids=[evidence5.id, evidence6.id, evidence7.id, evidence8.id],
        generated_by="claude-opus-4",
        is_ai_generated=True,
        is_reviewed=False,
    )
    session.add(analysis2)

    session.commit()
    session.close()

    print(f"✓ 数据库初始化完成：{db_path}")
    print(f"✓ 已添加 2 家公司：世纪互联、广联达")
    print(f"✓ 已添加 4 位高管")
    print(f"✓ 已添加 6 条行业动态")
    print(f"✓ 已添加 8 个证据片段")
    print(f"✓ 已添加 2 份 AI 分析摘要")


if __name__ == "__main__":
    seed_database()

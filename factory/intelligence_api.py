# ---------- 商业情报 API ----------


def intelligence_list_companies(db_path: str | Path, request_path: str) -> tuple[int, dict]:
    """公司列表，支持筛选"""
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(request_path).query)
    industry = query.get("industry", [None])[0]
    is_listed = query.get("is_listed", [None])[0]

    Session = init_intelligence_db(str(db_path))
    session = Session()

    try:
        q = session.query(Company)
        if industry:
            q = q.filter(Company.industry.like(f"%{industry}%"))
        if is_listed is not None:
            q = q.filter(Company.is_listed == (is_listed.lower() == "true"))

        companies = q.all()
        result = {
            "total": len(companies),
            "companies": [
                {
                    "id": c.id,
                    "name": c.name,
                    "short_name": c.short_name,
                    "stock_code": c.stock_code,
                    "industry": c.industry,
                    "is_listed": c.is_listed,
                    "market_cap": c.market_cap,
                    "website": c.website,
                }
                for c in companies
            ],
        }
        return 200, result
    finally:
        session.close()


def intelligence_company_detail(db_path: str | Path, company_id: int) -> tuple[int, dict]:
    """公司详情"""
    Session = init_intelligence_db(str(db_path))
    session = Session()

    try:
        company = session.query(Company).filter(Company.id == company_id).first()
        if not company:
            return 404, {"error": "公司不存在"}

        # 获取关联数据统计
        personnel_count = session.query(Personnel).filter(Personnel.company_id == company_id).count()
        dynamics_count = session.query(IndustryDynamic).filter(IndustryDynamic.company_id == company_id).count()

        result = {
            "id": company.id,
            "name": company.name,
            "name_en": company.name_en,
            "short_name": company.short_name,
            "stock_code": company.stock_code,
            "stock_exchange": company.stock_exchange,
            "industry": company.industry,
            "business_scope": company.business_scope,
            "founded_date": company.founded_date,
            "headquarters": company.headquarters,
            "website": company.website,
            "description": company.description,
            "is_listed": company.is_listed,
            "market_cap": company.market_cap,
            "employee_count": company.employee_count,
            "business_lines": company.business_lines,
            "org_structure": company.org_structure,
            "personnel_count": personnel_count,
            "dynamics_count": dynamics_count,
        }
        return 200, result
    finally:
        session.close()


def intelligence_list_personnel(db_path: str | Path, request_path: str) -> tuple[int, dict]:
    """人物列表，支持按公司筛选"""
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(request_path).query)
    company_id = query.get("company_id", [None])[0]
    is_executive = query.get("is_executive", [None])[0]

    Session = init_intelligence_db(str(db_path))
    session = Session()

    try:
        q = session.query(Personnel)
        if company_id:
            q = q.filter(Personnel.company_id == int(company_id))
        if is_executive is not None:
            q = q.filter(Personnel.is_executive == (is_executive.lower() == "true"))

        personnel = q.all()
        result = {
            "total": len(personnel),
            "personnel": [
                {
                    "id": p.id,
                    "name": p.name,
                    "title": p.title,
                    "department": p.department,
                    "is_executive": p.is_executive,
                    "company_id": p.company_id,
                    "company_name": p.company.name if p.company else None,
                }
                for p in personnel
            ],
        }
        return 200, result
    finally:
        session.close()


def intelligence_personnel_detail(db_path: str | Path, personnel_id: int) -> tuple[int, dict]:
    """人物详情"""
    Session = init_intelligence_db(str(db_path))
    session = Session()

    try:
        person = session.query(Personnel).filter(Personnel.id == personnel_id).first()
        if not person:
            return 404, {"error": "人物不存在"}

        publications = session.query(Publication).filter(Publication.author_id == personnel_id).all()

        result = {
            "id": person.id,
            "name": person.name,
            "name_en": person.name_en,
            "company_id": person.company_id,
            "company_name": person.company.name if person.company else None,
            "title": person.title,
            "department": person.department,
            "is_executive": person.is_executive,
            "career_history": person.career_history,
            "education": person.education,
            "expertise": person.expertise,
            "bio": person.bio,
            "linkedin_url": person.linkedin_url,
            "weibo_url": person.weibo_url,
            "publications": [
                {
                    "id": pub.id,
                    "title": pub.title,
                    "content_type": pub.content_type,
                    "published_date": pub.published_date,
                    "source_url": pub.source_url,
                    "topics": pub.topics,
                }
                for pub in publications
            ],
        }
        return 200, result
    finally:
        session.close()


def intelligence_list_dynamics(db_path: str | Path, request_path: str) -> tuple[int, dict]:
    """行业动态列表，支持筛选"""
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(request_path).query)
    company_id = query.get("company_id", [None])[0]
    type_filter = query.get("type", [None])[0]
    limit = int(query.get("limit", ["50"])[0])

    Session = init_intelligence_db(str(db_path))
    session = Session()

    try:
        q = session.query(IndustryDynamic).order_by(IndustryDynamic.published_date.desc())
        if company_id:
            q = q.filter(IndustryDynamic.company_id == int(company_id))
        if type_filter:
            q = q.filter(IndustryDynamic.type == type_filter)

        dynamics = q.limit(limit).all()
        result = {
            "total": len(dynamics),
            "dynamics": [
                {
                    "id": d.id,
                    "company_id": d.company_id,
                    "company_name": d.company.name if d.company else None,
                    "type": d.type,
                    "title": d.title,
                    "summary": d.summary,
                    "published_date": d.published_date,
                    "source_name": d.source_name,
                    "tags": d.tags,
                }
                for d in dynamics
            ],
        }
        return 200, result
    finally:
        session.close()


def intelligence_dynamic_detail(db_path: str | Path, dynamic_id: int) -> tuple[int, dict]:
    """动态详情"""
    Session = init_intelligence_db(str(db_path))
    session = Session()

    try:
        dynamic = session.query(IndustryDynamic).filter(IndustryDynamic.id == dynamic_id).first()
        if not dynamic:
            return 404, {"error": "动态不存在"}

        evidence = session.query(Evidence).filter(Evidence.dynamic_id == dynamic_id).all()

        result = {
            "id": dynamic.id,
            "company_id": dynamic.company_id,
            "company_name": dynamic.company.name if dynamic.company else None,
            "type": dynamic.type,
            "title": dynamic.title,
            "summary": dynamic.summary,
            "content": dynamic.content,
            "published_date": dynamic.published_date,
            "source_url": dynamic.source_url,
            "source_name": dynamic.source_name,
            "source_type": dynamic.source_type,
            "tags": dynamic.tags,
            "financial_period": dynamic.financial_period,
            "revenue": dynamic.revenue,
            "profit": dynamic.profit,
            "evidence": [
                {
                    "id": e.id,
                    "text": e.text,
                    "category": e.category,
                    "confidence": e.confidence,
                }
                for e in evidence
            ],
        }
        return 200, result
    finally:
        session.close()


def intelligence_search(db_path: str | Path, request_path: str) -> tuple[int, dict]:
    """全文搜索"""
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(request_path).query)
    q = query.get("q", [""])[0]
    limit = int(query.get("limit", ["20"])[0])

    if not q:
        return 400, {"error": "缺少搜索关键词"}

    Session = init_intelligence_db(str(db_path))
    session = Session()

    try:
        # 搜索公司
        companies = (
            session.query(Company)
            .filter(
                (Company.name.like(f"%{q}%"))
                | (Company.description.like(f"%{q}%"))
                | (Company.business_scope.like(f"%{q}%"))
            )
            .limit(limit)
            .all()
        )

        # 搜索动态
        dynamics = (
            session.query(IndustryDynamic)
            .filter(
                (IndustryDynamic.title.like(f"%{q}%"))
                | (IndustryDynamic.summary.like(f"%{q}%"))
                | (IndustryDynamic.content.like(f"%{q}%"))
            )
            .order_by(IndustryDynamic.published_date.desc())
            .limit(limit)
            .all()
        )

        # 搜索证据
        evidence = (
            session.query(Evidence)
            .filter((Evidence.text.like(f"%{q}%")) | (Evidence.context.like(f"%{q}%")))
            .limit(limit)
            .all()
        )

        result = {
            "query": q,
            "companies": [
                {
                    "id": c.id,
                    "name": c.name,
                    "industry": c.industry,
                    "description": c.description[:200] if c.description else "",
                }
                for c in companies
            ],
            "dynamics": [
                {
                    "id": d.id,
                    "type": d.type,
                    "title": d.title,
                    "company_name": d.company.name if d.company else None,
                    "published_date": d.published_date,
                }
                for d in dynamics
            ],
            "evidence": [
                {
                    "id": e.id,
                    "text": e.text,
                    "category": e.category,
                    "company_name": e.company.name if e.company else None,
                    "source_url": e.source_url,
                }
                for e in evidence
            ],
        }
        return 200, result
    finally:
        session.close()


def intelligence_list_analyses(db_path: str | Path, request_path: str) -> tuple[int, dict]:
    """分析列表"""
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(request_path).query)
    company_id = query.get("company_id", [None])[0]

    Session = init_intelligence_db(str(db_path))
    session = Session()

    try:
        q = session.query(Analysis).order_by(Analysis.generated_at.desc())
        if company_id:
            q = q.filter(Analysis.company_id == int(company_id))

        analyses = q.all()
        result = {
            "total": len(analyses),
            "analyses": [
                {
                    "id": a.id,
                    "company_id": a.company_id,
                    "company_name": session.query(Company)
                    .filter(Company.id == a.company_id)
                    .first()
                    .name,
                    "title": a.title,
                    "opportunity_type": a.opportunity_type,
                    "is_ai_generated": a.is_ai_generated,
                    "is_reviewed": a.is_reviewed,
                    "generated_at": a.generated_at.isoformat(),
                }
                for a in analyses
            ],
        }
        return 200, result
    finally:
        session.close()


def intelligence_analysis_detail(db_path: str | Path, analysis_id: int) -> tuple[int, dict]:
    """分析详情，包含完整摘要和证据"""
    Session = init_intelligence_db(str(db_path))
    session = Session()

    try:
        analysis = session.query(Analysis).filter(Analysis.id == analysis_id).first()
        if not analysis:
            return 404, {"error": "分析不存在"}

        # 获取证据
        evidence_list = (
            session.query(Evidence).filter(Evidence.id.in_(analysis.evidence_ids)).all()
        )

        company = session.query(Company).filter(Company.id == analysis.company_id).first()

        result = {
            "id": analysis.id,
            "company_id": analysis.company_id,
            "company_name": company.name if company else None,
            "title": analysis.title,
            "summary": analysis.summary,
            "opportunity_type": analysis.opportunity_type,
            "generated_by": analysis.generated_by,
            "generated_at": analysis.generated_at.isoformat(),
            "is_ai_generated": analysis.is_ai_generated,
            "is_reviewed": analysis.is_reviewed,
            "reviewed_by": analysis.reviewed_by,
            "review_notes": analysis.review_notes,
            "evidence": [
                {
                    "id": e.id,
                    "text": e.text,
                    "context": e.context,
                    "source_url": e.source_url,
                    "source_name": e.source_name,
                    "category": e.category,
                    "confidence": e.confidence,
                    "topics": e.topics,
                }
                for e in evidence_list
            ],
        }
        return 200, result
    finally:
        session.close()

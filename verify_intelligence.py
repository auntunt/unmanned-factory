#!/usr/bin/env python3
"""验证商业情报系统是否正确安装和配置"""

import sys
from pathlib import Path


def check_models():
    """检查数据模型"""
    try:
        from factory.intelligence.models import Company, Personnel, init_db
        print("✓ 数据模型导入成功")
        return True
    except Exception as e:
        print(f"✗ 数据模型导入失败: {e}")
        return False


def check_collectors():
    """检查采集器"""
    try:
        from factory.intelligence.collectors import (
            CompanyInfoCollector,
            FinancialReportCollector,
        )
        print("✓ 数据采集器导入成功")
        return True
    except Exception as e:
        print(f"✗ 数据采集器导入失败: {e}")
        return False


def check_api():
    """检查 API 端点"""
    try:
        from factory.intelligence_api import (
            intelligence_list_companies,
            intelligence_search,
        )
        print("✓ API 端点导入成功")
        return True
    except Exception as e:
        print(f"✗ API 端点导入失败: {e}")
        return False


def check_database():
    """检查数据库"""
    db_path = Path("intelligence.db")
    if not db_path.exists():
        print("ℹ 数据库文件不存在，请运行: uv run python factory/intelligence/seed_data.py")
        return False

    try:
        from factory.intelligence.models import Company, init_db

        Session = init_db(str(db_path))
        session = Session()
        count = session.query(Company).count()
        session.close()
        print(f"✓ 数据库包含 {count} 家公司")
        return True
    except Exception as e:
        print(f"✗ 数据库检查失败: {e}")
        return False


def check_frontend():
    """检查前端文件"""
    frontend_files = [
        "frontend/src/pages/intelligence/Home.tsx",
        "frontend/src/pages/intelligence/CompanyList.tsx",
        "frontend/src/pages/intelligence/CompanyDetail.tsx",
        "frontend/src/pages/intelligence/AnalysisWorkbench.tsx",
    ]
    all_exist = True
    for file in frontend_files:
        if Path(file).exists():
            print(f"✓ {file}")
        else:
            print(f"✗ {file} 不存在")
            all_exist = False
    return all_exist


def main():
    print("=" * 60)
    print("商业情报系统验证")
    print("=" * 60)

    checks = [
        ("数据模型", check_models),
        ("数据采集器", check_collectors),
        ("API 端点", check_api),
        ("数据库", check_database),
        ("前端文件", check_frontend),
    ]

    results = []
    for name, check_fn in checks:
        print(f"\n检查 {name}:")
        results.append(check_fn())

    print("\n" + "=" * 60)
    if all(results):
        print("✓ 所有检查通过！")
        print("\n下一步:")
        print("1. 生成示例数据: uv run python factory/intelligence/seed_data.py")
        print("2. 启动后端: uv run factory api --intelligence-db intelligence.db")
        print("3. 启动前端: cd frontend && npm install && npm run dev")
        print("4. 访问: http://localhost:5173/intelligence")
        return 0
    else:
        print("✗ 部分检查失败，请查看上面的错误信息")
        return 1


if __name__ == "__main__":
    sys.exit(main())

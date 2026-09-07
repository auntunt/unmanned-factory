# 商业情报分析系统 MVP

中国商业情报分析系统，为 B2B 销售和商业研究团队提供公司档案、关键人物、行业动态和分析工作台。

## 功能特性

### 1. 公司档案
- 业务线、组织架构、上市公司基本信息
- 目标公司：世纪互联、广联达（可扩展）
- 股票代码、市值、员工数等基本信息

### 2. 关键人物档案
- 高管和业务负责人
- 公开职业履历、教育背景
- 公开发表内容（采访、演讲、文章）
- 专业领域和关注主题

### 3. 行业动态
- 公司公告、财务报告
- 行业新闻、招聘信息
- 监管政策和行业变化

### 4. 分析工作台
- 全文搜索（公司/动态/证据）
- 筛选和时间线
- 证据片段展示（可追溯来源）
- 基于证据的 AI 分析摘要

## 安全和隐私

- ✅ **仅公开信息**：只收集公开可访问的职业信息
- ✅ **隐私保护**：不收集或推断私人敏感信息
- ✅ **可追溯性**：所有事实和分析都可追溯到原始来源
- ✅ **明确区分**：事实与 AI 生成内容明确标识
- ✅ **登录墙检测**：遇到登录墙、反爬或不可访问来源会停止并标记

## 快速开始

### 1. 初始化数据库和示例数据

```bash
# 安装依赖
uv sync

# 生成示例数据（世纪互联、广联达）
uv run python factory/intelligence/seed_data.py
```

这将创建 `intelligence.db` 并填充：
- 2 家公司档案
- 4 位高管信息
- 6 条行业动态
- 8 个证据片段
- 2 份 AI 分析摘要

### 2. 启动后端 API

```bash
# 启动 API 服务（包含工厂任务 API 和商业情报 API）
uv run python -m factory.cli api \
  --db audit.db \
  --queue ~/.factory/q \
  --intelligence-db intelligence.db \
  --port 8788
```

API 端点：
- `http://127.0.0.1:8788/api/intelligence/companies` - 公司列表
- `http://127.0.0.1:8788/api/intelligence/company/:id` - 公司详情
- `http://127.0.0.1:8788/api/intelligence/dynamics` - 行业动态
- `http://127.0.0.1:8788/api/intelligence/search?q=关键词` - 全文搜索
- `http://127.0.0.1:8788/api/intelligence/analyses` - AI 分析列表

### 3. 启动前端

```bash
cd frontend
npm install
npm run dev
```

前端访问地址：`http://localhost:5173`

点击顶部导航的 **"商业情报"** 进入系统。

## 数据采集

系统提供可扩展的 Playwright 数据采集适配器：

```python
from factory.intelligence.collectors import CompanyInfoCollector, FinancialReportCollector

# 采集公司信息
collector = CompanyInfoCollector()
result = await collector.collect(stock_code="002410", exchange="SZSE")

# 采集财报
report_collector = FinancialReportCollector()
result = await report_collector.collect(stock_code="002410", year="2024", quarter="Q3")
```

**安全特性**：
- 自动检测登录墙并停止
- 自动检测反爬机制并标记
- 只采集公开可访问的内容

## 项目结构

```
factory/intelligence/
├── models.py          # SQLAlchemy 数据模型
├── seed_data.py       # 示例数据生成脚本
├── collectors.py      # Playwright 数据采集适配器
└── __init__.py

factory/
├── intelligence_api.py  # 商业情报 API 端点
└── api.py              # 主 API 路由（集成商业情报）

frontend/src/pages/intelligence/
├── Home.tsx            # 商业情报首页
├── CompanyList.tsx     # 公司列表
├── CompanyDetail.tsx   # 公司详情
└── AnalysisWorkbench.tsx  # 分析工作台

intelligence.db         # SQLite 数据库（自动创建）
```

## 数据模型

### Company（公司）
- 基本信息：名称、股票代码、行业、市值
- 业务线：JSON 格式，包含营收占比
- 组织架构：JSON 格式，部门和负责人

### Personnel（人物）
- 基本信息：姓名、职位、部门
- 职业履历：JSON 格式
- 教育背景：JSON 格式
- 专业领域：数组

### IndustryDynamic（行业动态）
- 类型：announcement/financial/news/hiring/policy
- 内容：标题、摘要、完整内容
- 来源：URL、名称、类型
- 财报字段：营收、利润、财报期

### Evidence（证据）
- 原文片段和上下文
- 来源 URL（必须）
- 分类：opportunity/risk/trend/fact
- 置信度：high/medium/low

### Analysis（分析）
- AI 生成的分析摘要
- 证据引用（ID 列表）
- 机会类型：partnership/sales/investment
- 明确标识 AI 生成

## 扩展到其他公司

修改 `factory/intelligence/seed_data.py` 添加新公司：

```python
new_company = Company(
    name="新公司名称",
    stock_code="000001",
    stock_exchange="SZSE",
    industry="所属行业",
    # ... 其他字段
)
session.add(new_company)
```

## API 集成到 CLI

修改 `factory/cli.py` 添加 `--intelligence-db` 参数：

```python
@click.option("--intelligence-db", type=click.Path(), help="商业情报数据库路径")
def api_command(db, queue, port, intelligence_db):
    from factory.api import serve_api
    serve_api(db, queue, port=port, intelligence_db=intelligence_db)
```

## 验收检查

运行以下命令验证系统：

```bash
# 1. 检查数据库已创建并包含数据
uv run python -c "
from factory.intelligence.models import init_db, Company
Session = init_db('intelligence.db')
session = Session()
print(f'公司数量: {session.query(Company).count()}')
session.close()
"

# 2. 启动 API 并访问
curl http://127.0.0.1:8788/api/intelligence/companies

# 3. 搜索测试
curl "http://127.0.0.1:8788/api/intelligence/search?q=液冷"

# 4. 检查前端路由
# 访问 http://localhost:5173/intelligence
```

## 开发和测试

```bash
# 运行测试
uv run pytest tests/test_intelligence.py

# 重新生成示例数据
uv run python factory/intelligence/seed_data.py
```

## 限制和未来扩展

当前 MVP 限制：
- 示例数据仅包含 2 家公司
- 数据采集适配器为框架代码，需补充实际页面解析逻辑
- 全文搜索使用 SQLite LIKE，大规模数据建议用 Elasticsearch

未来扩展方向：
- 更多上市公司数据
- 实时数据采集任务调度
- 更复杂的关系图谱
- 行业分析报告生成
- 导出功能（PDF/Excel）

## 许可和使用

本系统仅用于合法商业研究目的。使用时请遵守：
- 仅采集公开可访问的信息
- 遵守目标网站的 robots.txt 和使用条款
- 不绕过登录墙或反爬机制
- 尊重个人隐私，不收集敏感信息

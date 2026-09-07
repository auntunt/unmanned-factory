# 商业情报分析系统 - 快速启动指南

## 一键验证安装

```bash
uv run python verify_intelligence.py
```

## 快速启动（3 步）

### 1. 生成示例数据

```bash
uv sync
uv run python factory/intelligence/seed_data.py
```

输出：
```
✓ 数据库初始化完成：intelligence.db
✓ 已添加 2 家公司：世纪互联、广联达
✓ 已添加 4 位高管
✓ 已添加 6 条行业动态
✓ 已添加 8 个证据片段
✓ 已添加 2 份 AI 分析摘要
```

### 2. 启动后端 API

```bash
uv run factory api --intelligence-db intelligence.db
```

或使用默认队列：
```bash
mkdir -p ~/.factory/q
uv run factory api --db audit.db --queue ~/.factory/q --intelligence-db intelligence.db
```

### 3. 启动前端

```bash
cd frontend
npm install
npm run dev
```

访问 http://localhost:5173 → 点击顶部 **"商业情报"** 导航链接

## 功能演示

1. **公司列表**：查看世纪互联和广联达的基本信息
2. **公司详情**：业务线、组织架构、高管团队
3. **分析工作台**：
   - 搜索 "液冷" → 查看相关动态和证据
   - 搜索 "云转型" → 查看广联达云化进展
   - 查看 AI 生成的分析摘要（带证据溯源）

## API 端点测试

```bash
# 公司列表
curl http://127.0.0.1:8788/api/intelligence/companies

# 公司详情
curl http://127.0.0.1:8788/api/intelligence/company/1

# 全文搜索
curl "http://127.0.0.1:8788/api/intelligence/search?q=液冷"

# AI 分析列表
curl http://127.0.0.1:8788/api/intelligence/analyses
```

## 运行测试

```bash
# 运行商业情报测试
uv run pytest tests/test_intelligence.py -v

# 运行所有测试
uv run pytest -m "not smoke"
```

## 目录结构

```
factory/intelligence/
├── models.py           # SQLAlchemy 数据模型
├── seed_data.py        # 示例数据生成
├── collectors.py       # Playwright 数据采集
└── __init__.py

factory/
├── intelligence_api.py # 商业情报 API 端点
└── cli.py             # CLI（已添加 --intelligence-db）

frontend/src/pages/intelligence/
├── Home.tsx           # 商业情报首页
├── CompanyList.tsx    # 公司列表
├── CompanyDetail.tsx  # 公司详情
└── AnalysisWorkbench.tsx  # 分析工作台

intelligence.db        # SQLite 数据库（自动创建）
```

## 疑难排查

### 前端无法连接 API
确保后端在运行并且使用正确的端口（默认 8788）

### 数据库为空
运行 `uv run python factory/intelligence/seed_data.py` 生成示例数据

### 前端页面空白
检查浏览器控制台，确保 API 返回数据格式正确

## 扩展开发

### 添加新公司
编辑 `factory/intelligence/seed_data.py`，参考世纪互联和广联达的数据结构

### 添加新的采集器
在 `factory/intelligence/collectors.py` 中添加新的 Collector 类

### 添加新的前端页面
在 `frontend/src/pages/intelligence/` 创建新组件，并在 `App.tsx` 添加路由

## 完整文档

详细文档请参考 `docs/INTELLIGENCE.md`

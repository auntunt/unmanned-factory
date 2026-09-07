import React from 'react';
import { Link } from 'react-router-dom';

export default function IntelligenceHome() {
  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-3xl font-bold text-slate-900">商业情报分析系统</h1>
        <p className="mt-2 text-slate-600">
          为 B2B 销售和商业研究团队提供公司档案、关键人物、行业动态和分析工作台
        </p>
      </div>

      <div className="grid grid-cols-1 gap-6 md:grid-cols-2">
        <Link
          to="/intelligence/companies"
          className="rounded-lg border border-slate-200 bg-white p-6 hover:border-blue-300 hover:shadow-md transition"
        >
          <div className="flex items-start">
            <div className="rounded-lg bg-blue-100 p-3">
              <svg className="h-6 w-6 text-blue-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4" />
              </svg>
            </div>
            <div className="ml-4 flex-1">
              <h2 className="text-lg font-semibold text-slate-900">公司档案</h2>
              <p className="mt-1 text-sm text-slate-600">
                业务线、组织架构、上市公司基本信息
              </p>
              <p className="mt-2 text-sm text-blue-600 font-medium">查看公司 →</p>
            </div>
          </div>
        </Link>

        <Link
          to="/intelligence/workbench"
          className="rounded-lg border border-slate-200 bg-white p-6 hover:border-green-300 hover:shadow-md transition"
        >
          <div className="flex items-start">
            <div className="rounded-lg bg-green-100 p-3">
              <svg className="h-6 w-6 text-green-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
              </svg>
            </div>
            <div className="ml-4 flex-1">
              <h2 className="text-lg font-semibold text-slate-900">分析工作台</h2>
              <p className="mt-1 text-sm text-slate-600">
                全文搜索、筛选、时间线、证据展示、AI 分析摘要
              </p>
              <p className="mt-2 text-sm text-green-600 font-medium">开始分析 →</p>
            </div>
          </div>
        </Link>
      </div>

      <div className="rounded-lg border border-slate-200 bg-white p-6">
        <h2 className="text-lg font-semibold text-slate-900 mb-4">系统说明</h2>
        <div className="space-y-3 text-sm text-slate-600">
          <div className="flex items-start">
            <span className="text-blue-600 mr-2">✓</span>
            <p><strong>目标公司：</strong>世纪互联、广联达（可扩展到其他上市公司）</p>
          </div>
          <div className="flex items-start">
            <span className="text-blue-600 mr-2">✓</span>
            <p><strong>数据来源：</strong>仅收集公开信息（官网、财报、新闻、招聘等）</p>
          </div>
          <div className="flex items-start">
            <span className="text-blue-600 mr-2">✓</span>
            <p><strong>隐私保护：</strong>只包含公开职业信息，不收集或推断私人敏感信息</p>
          </div>
          <div className="flex items-start">
            <span className="text-blue-600 mr-2">✓</span>
            <p><strong>可追溯性：</strong>所有事实和分析都可追溯到原始来源</p>
          </div>
          <div className="flex items-start">
            <span className="text-blue-600 mr-2">✓</span>
            <p><strong>AI 分析：</strong>基于证据的分析摘要，事实与 AI 生成内容明确区分</p>
          </div>
        </div>
      </div>
    </div>
  );
}

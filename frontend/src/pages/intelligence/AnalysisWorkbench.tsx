import React, { useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import axios from 'axios';

interface SearchResult {
  companies: Array<{
    id: number;
    name: string;
    industry: string;
    description: string;
  }>;
  dynamics: Array<{
    id: number;
    type: string;
    title: string;
    company_name: string;
    published_date: string;
  }>;
  evidence: Array<{
    id: number;
    text: string;
    category: string;
    company_name: string;
    source_url: string;
  }>;
}

interface Analysis {
  id: number;
  company_id: number;
  company_name: string;
  title: string;
  opportunity_type: string;
  is_ai_generated: boolean;
  generated_at: string;
}

export default function AnalysisWorkbench() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [searchQuery, setSearchQuery] = useState(searchParams.get('q') || '');
  const [searchResults, setSearchResults] = useState<SearchResult | null>(null);
  const [analyses, setAnalyses] = useState<Analysis[]>([]);
  const [loading, setLoading] = useState(false);

  const companyIdFilter = searchParams.get('company_id');

  useEffect(() => {
    // 加载 AI 分析
    const url = companyIdFilter
      ? `http://127.0.0.1:8788/api/intelligence/analyses?company_id=${companyIdFilter}`
      : 'http://127.0.0.1:8788/api/intelligence/analyses';

    axios.get(url).then((res) => {
      setAnalyses(res.data.analyses || []);
    });
  }, [companyIdFilter]);

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault();
    if (!searchQuery.trim()) return;

    setLoading(true);
    axios
      .get(`http://127.0.0.1:8788/api/intelligence/search?q=${encodeURIComponent(searchQuery)}`)
      .then((res) => {
        setSearchResults(res.data);
        setLoading(false);
      })
      .catch(() => {
        setLoading(false);
      });
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-slate-900">分析工作台</h1>
          <p className="mt-1 text-sm text-slate-600">全文搜索、证据展示、AI 分析摘要</p>
        </div>
        <Link to="/intelligence" className="text-sm text-slate-600 hover:text-slate-900">
          ← 返回
        </Link>
      </div>

      {/* 搜索框 */}
      <div className="rounded-lg border border-slate-200 bg-white p-6">
        <form onSubmit={handleSearch} className="flex gap-3">
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="搜索公司、动态、证据..."
            className="flex-1 rounded-lg border border-slate-300 px-4 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-200"
          />
          <button
            type="submit"
            className="rounded-lg bg-blue-600 px-6 py-2 text-sm font-medium text-white hover:bg-blue-700"
          >
            搜索
          </button>
        </form>
      </div>

      {/* 搜索结果 */}
      {loading && <div className="text-center py-8">搜索中...</div>}

      {searchResults && (
        <div className="space-y-6">
          {/* 公司结果 */}
          {searchResults.companies.length > 0 && (
            <div className="rounded-lg border border-slate-200 bg-white p-6">
              <h2 className="text-lg font-semibold text-slate-900 mb-4">
                公司 ({searchResults.companies.length})
              </h2>
              <div className="space-y-3">
                {searchResults.companies.map((company) => (
                  <Link
                    key={company.id}
                    to={`/intelligence/company/${company.id}`}
                    className="block rounded-lg border border-slate-200 p-4 hover:border-blue-300 hover:bg-slate-50"
                  >
                    <h3 className="font-semibold text-slate-900">{company.name}</h3>
                    <p className="text-sm text-slate-600 mt-1">{company.industry}</p>
                    {company.description && (
                      <p className="text-sm text-slate-500 mt-2 line-clamp-2">
                        {company.description}
                      </p>
                    )}
                  </Link>
                ))}
              </div>
            </div>
          )}

          {/* 动态结果 */}
          {searchResults.dynamics.length > 0 && (
            <div className="rounded-lg border border-slate-200 bg-white p-6">
              <h2 className="text-lg font-semibold text-slate-900 mb-4">
                行业动态 ({searchResults.dynamics.length})
              </h2>
              <div className="space-y-3">
                {searchResults.dynamics.map((dynamic) => (
                  <div
                    key={dynamic.id}
                    className="rounded-lg border border-slate-200 p-4"
                  >
                    <div className="flex items-start justify-between">
                      <div className="flex-1">
                        <div className="flex items-center gap-2">
                          <span className="inline-flex items-center rounded-full bg-blue-100 px-2 py-0.5 text-xs font-medium text-blue-800">
                            {dynamic.type}
                          </span>
                          <span className="text-sm text-slate-600">
                            {dynamic.company_name}
                          </span>
                        </div>
                        <h3 className="font-semibold text-slate-900 mt-2">
                          {dynamic.title}
                        </h3>
                        <p className="text-sm text-slate-500 mt-1">
                          {dynamic.published_date}
                        </p>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* 证据结果 */}
          {searchResults.evidence.length > 0 && (
            <div className="rounded-lg border border-slate-200 bg-white p-6">
              <h2 className="text-lg font-semibold text-slate-900 mb-4">
                证据片段 ({searchResults.evidence.length})
              </h2>
              <div className="space-y-3">
                {searchResults.evidence.map((evidence) => (
                  <div
                    key={evidence.id}
                    className="rounded-lg border border-slate-200 bg-amber-50 p-4"
                  >
                    <div className="flex items-start gap-3">
                      <div className="flex-shrink-0">
                        <span className="inline-flex items-center rounded-full bg-amber-100 px-2.5 py-0.5 text-xs font-medium text-amber-800">
                          {evidence.category}
                        </span>
                      </div>
                      <div className="flex-1">
                        <p className="text-sm text-slate-900">{evidence.text}</p>
                        <div className="mt-2 flex items-center gap-3 text-xs text-slate-600">
                          <span>{evidence.company_name}</span>
                          <a
                            href={evidence.source_url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-blue-600 hover:underline"
                          >
                            查看来源 →
                          </a>
                        </div>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* AI 分析摘要 */}
      <div className="rounded-lg border border-slate-200 bg-white p-6">
        <h2 className="text-lg font-semibold text-slate-900 mb-4">AI 分析摘要</h2>
        {analyses.length === 0 ? (
          <p className="text-sm text-slate-600">暂无分析</p>
        ) : (
          <div className="space-y-3">
            {analyses.map((analysis) => (
              <Link
                key={analysis.id}
                to={`/intelligence/analysis/${analysis.id}`}
                className="block rounded-lg border border-slate-200 p-4 hover:border-green-300 hover:bg-slate-50"
              >
                <div className="flex items-start justify-between">
                  <div className="flex-1">
                    <div className="flex items-center gap-2">
                      {analysis.is_ai_generated && (
                        <span className="inline-flex items-center rounded-full bg-purple-100 px-2 py-0.5 text-xs font-medium text-purple-800">
                          AI 生成
                        </span>
                      )}
                      <span className="inline-flex items-center rounded-full bg-green-100 px-2 py-0.5 text-xs font-medium text-green-800">
                        {analysis.opportunity_type}
                      </span>
                    </div>
                    <h3 className="font-semibold text-slate-900 mt-2">{analysis.title}</h3>
                    <p className="text-sm text-slate-600 mt-1">{analysis.company_name}</p>
                  </div>
                  <svg
                    className="h-5 w-5 text-slate-400 flex-shrink-0"
                    fill="none"
                    viewBox="0 0 24 24"
                    stroke="currentColor"
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      strokeWidth={2}
                      d="M9 5l7 7-7 7"
                    />
                  </svg>
                </div>
              </Link>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

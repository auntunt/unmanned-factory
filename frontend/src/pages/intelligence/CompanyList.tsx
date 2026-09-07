import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import axios from 'axios';

interface Company {
  id: number;
  name: string;
  short_name: string;
  stock_code: string;
  industry: string;
  is_listed: boolean;
  market_cap: number;
  website: string;
}

export default function CompanyList() {
  const [companies, setCompanies] = useState<Company[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    axios
      .get('http://127.0.0.1:8788/api/intelligence/companies')
      .then((res) => {
        setCompanies(res.data.companies || []);
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message);
        setLoading(false);
      });
  }, []);

  if (loading) {
    return <div className="text-center py-12">加载中...</div>;
  }

  if (error) {
    return (
      <div className="rounded-lg border border-red-200 bg-red-50 p-6">
        <p className="text-red-800">加载失败：{error}</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-slate-900">公司档案</h1>
          <p className="mt-1 text-sm text-slate-600">共 {companies.length} 家公司</p>
        </div>
        <Link
          to="/intelligence"
          className="text-sm text-slate-600 hover:text-slate-900"
        >
          ← 返回
        </Link>
      </div>

      <div className="grid grid-cols-1 gap-4">
        {companies.map((company) => (
          <Link
            key={company.id}
            to={`/intelligence/company/${company.id}`}
            className="rounded-lg border border-slate-200 bg-white p-6 hover:border-blue-300 hover:shadow-md transition"
          >
            <div className="flex items-start justify-between">
              <div className="flex-1">
                <div className="flex items-center gap-3">
                  <h3 className="text-lg font-semibold text-slate-900">
                    {company.name}
                  </h3>
                  {company.short_name && (
                    <span className="text-sm text-slate-600">
                      ({company.short_name})
                    </span>
                  )}
                  {company.is_listed && (
                    <span className="inline-flex items-center rounded-full bg-blue-100 px-2.5 py-0.5 text-xs font-medium text-blue-800">
                      上市公司
                    </span>
                  )}
                </div>
                <div className="mt-2 flex items-center gap-4 text-sm text-slate-600">
                  {company.stock_code && (
                    <span>股票代码：{company.stock_code}</span>
                  )}
                  <span>行业：{company.industry}</span>
                  {company.market_cap && (
                    <span>市值：{company.market_cap} 亿元</span>
                  )}
                </div>
                {company.website && (
                  <div className="mt-2 text-sm text-blue-600">
                    {company.website}
                  </div>
                )}
              </div>
              <svg
                className="h-5 w-5 text-slate-400"
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
    </div>
  );
}

import React, { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import axios from 'axios';

interface CompanyDetail {
  id: number;
  name: string;
  name_en: string;
  short_name: string;
  stock_code: string;
  stock_exchange: string;
  industry: string;
  business_scope: string;
  founded_date: string;
  headquarters: string;
  website: string;
  description: string;
  is_listed: boolean;
  market_cap: number;
  employee_count: number;
  business_lines: Array<{ name: string; description: string; revenue_pct: number }>;
  org_structure: Array<{ dept: string; head: string; level: number; description: string }>;
  personnel_count: number;
  dynamics_count: number;
}

export default function CompanyDetail() {
  const { companyId } = useParams<{ companyId: string }>();
  const [company, setCompany] = useState<CompanyDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    axios
      .get(`http://127.0.0.1:8788/api/intelligence/company/${companyId}`)
      .then((res) => {
        setCompany(res.data);
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message);
        setLoading(false);
      });
  }, [companyId]);

  if (loading) {
    return <div className="text-center py-12">加载中...</div>;
  }

  if (error || !company) {
    return (
      <div className="rounded-lg border border-red-200 bg-red-50 p-6">
        <p className="text-red-800">加载失败：{error || '公司不存在'}</p>
        <Link to="/intelligence/companies" className="mt-3 inline-block text-sm text-red-700 hover:underline">
          返回列表
        </Link>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="text-3xl font-bold text-slate-900">{company.name}</h1>
            {company.is_listed && (
              <span className="inline-flex items-center rounded-full bg-blue-100 px-3 py-1 text-sm font-medium text-blue-800">
                上市公司
              </span>
            )}
          </div>
          {company.name_en && (
            <p className="mt-1 text-slate-600">{company.name_en}</p>
          )}
        </div>
        <Link
          to="/intelligence/companies"
          className="text-sm text-slate-600 hover:text-slate-900"
        >
          ← 返回列表
        </Link>
      </div>

      {/* 基本信息 */}
      <div className="rounded-lg border border-slate-200 bg-white p-6">
        <h2 className="text-lg font-semibold text-slate-900 mb-4">基本信息</h2>
        <div className="grid grid-cols-2 gap-4 text-sm">
          <div>
            <span className="text-slate-600">股票代码：</span>
            <span className="font-medium">{company.stock_code}</span>
          </div>
          <div>
            <span className="text-slate-600">交易所：</span>
            <span className="font-medium">{company.stock_exchange}</span>
          </div>
          <div>
            <span className="text-slate-600">所属行业：</span>
            <span className="font-medium">{company.industry}</span>
          </div>
          <div>
            <span className="text-slate-600">成立时间：</span>
            <span className="font-medium">{company.founded_date}</span>
          </div>
          <div>
            <span className="text-slate-600">总部：</span>
            <span className="font-medium">{company.headquarters}</span>
          </div>
          <div>
            <span className="text-slate-600">员工数：</span>
            <span className="font-medium">{company.employee_count?.toLocaleString()} 人</span>
          </div>
          {company.market_cap && (
            <div>
              <span className="text-slate-600">市值：</span>
              <span className="font-medium">{company.market_cap} 亿元</span>
            </div>
          )}
          {company.website && (
            <div className="col-span-2">
              <span className="text-slate-600">官网：</span>
              <a
                href={company.website}
                target="_blank"
                rel="noopener noreferrer"
                className="font-medium text-blue-600 hover:underline"
              >
                {company.website}
              </a>
            </div>
          )}
        </div>
        {company.description && (
          <div className="mt-4 pt-4 border-t border-slate-200">
            <p className="text-sm text-slate-700">{company.description}</p>
          </div>
        )}
      </div>

      {/* 业务线 */}
      {company.business_lines && company.business_lines.length > 0 && (
        <div className="rounded-lg border border-slate-200 bg-white p-6">
          <h2 className="text-lg font-semibold text-slate-900 mb-4">业务线</h2>
          <div className="space-y-3">
            {company.business_lines.map((line, idx) => (
              <div key={idx} className="rounded-lg bg-slate-50 p-4">
                <div className="flex items-center justify-between">
                  <h3 className="font-semibold text-slate-900">{line.name}</h3>
                  <span className="text-sm font-medium text-blue-600">
                    {line.revenue_pct}% 营收占比
                  </span>
                </div>
                <p className="mt-2 text-sm text-slate-600">{line.description}</p>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 组织架构 */}
      {company.org_structure && company.org_structure.length > 0 && (
        <div className="rounded-lg border border-slate-200 bg-white p-6">
          <h2 className="text-lg font-semibold text-slate-900 mb-4">组织架构</h2>
          <div className="space-y-3">
            {company.org_structure.map((dept, idx) => (
              <div key={idx} className="flex items-start gap-4 rounded-lg bg-slate-50 p-4">
                <div className="flex-shrink-0 w-8 h-8 rounded-full bg-blue-100 flex items-center justify-center text-sm font-semibold text-blue-700">
                  {dept.level}
                </div>
                <div className="flex-1">
                  <h3 className="font-semibold text-slate-900">{dept.dept}</h3>
                  <p className="text-sm text-slate-600 mt-1">{dept.description}</p>
                  {dept.head && (
                    <p className="text-sm text-slate-500 mt-1">负责人：{dept.head}</p>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 相关数据统计 */}
      <div className="grid grid-cols-2 gap-4">
        <Link
          to={`/intelligence/workbench?company_id=${company.id}`}
          className="rounded-lg border border-slate-200 bg-white p-6 hover:border-blue-300 hover:shadow-md transition"
        >
          <div className="text-sm text-slate-600">关键人物</div>
          <div className="mt-2 text-3xl font-bold text-slate-900">{company.personnel_count}</div>
          <div className="mt-2 text-sm text-blue-600">查看详情 →</div>
        </Link>
        <Link
          to={`/intelligence/workbench?company_id=${company.id}`}
          className="rounded-lg border border-slate-200 bg-white p-6 hover:border-green-300 hover:shadow-md transition"
        >
          <div className="text-sm text-slate-600">行业动态</div>
          <div className="mt-2 text-3xl font-bold text-slate-900">{company.dynamics_count}</div>
          <div className="mt-2 text-sm text-green-600">查看详情 →</div>
        </Link>
      </div>
    </div>
  );
}

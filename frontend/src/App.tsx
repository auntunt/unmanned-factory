import { createBrowserRouter, Link, RouterProvider, useRouteError } from 'react-router-dom'
import TaskList from './pages/TaskList'
import TaskDetail from './pages/TaskDetail'
import Stats from './pages/Stats'
import Submit from './pages/Submit'
import IntelligenceHome from './pages/intelligence/Home'
import CompanyList from './pages/intelligence/CompanyList'
import CompanyDetail from './pages/intelligence/CompanyDetail'
import AnalysisWorkbench from './pages/intelligence/AnalysisWorkbench'

/** 顶部导航 + 内容区。三个页面共用，所以放在路由的 element 外层。 */
function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-6xl items-center gap-6 px-6 py-4">
          <Link to="/" className="text-lg font-semibold tracking-tight">
            自动化无人工厂
          </Link>
          <nav className="flex gap-4 text-sm">
            <Link to="/" className="text-slate-600 hover:text-slate-900">
              任务
            </Link>
            <Link to="/submit" className="text-slate-600 hover:text-slate-900">
              投递
            </Link>
            <Link to="/stats" className="text-slate-600 hover:text-slate-900">
              统计
            </Link>
            <Link to="/intelligence" className="text-blue-600 hover:text-blue-900 font-semibold">
              商业情报
            </Link>
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-6 py-8">{children}</main>
    </div>
  )
}

/**
 * 路由级兜底。没有这个，页面组件里任何未捕获异常都会让 react-router
 * 渲染它自带的英文报错页 —— 对着演示屏幕看那个很难解释。
 */
function ErrorPage() {
  const err = useRouteError()
  const msg = err instanceof Error ? err.message : String(err)
  return (
    <Shell>
      <div className="rounded-lg border border-red-200 bg-red-50 p-6">
        <h2 className="text-lg font-semibold text-red-800">页面出错了</h2>
        <p className="mt-2 font-mono text-sm text-red-700">{msg}</p>
        <Link
          to="/"
          className="mt-4 inline-block rounded border border-red-300 bg-white px-3 py-1.5 text-sm text-red-800 hover:bg-red-100"
        >
          返回任务列表
        </Link>
      </div>
    </Shell>
  )
}

// 路由参数名必须是 taskId：TaskDetail 里用 useParams<{ taskId: string }>() 读。
const router = createBrowserRouter([
  {
    path: '/',
    element: (
      <Shell>
        <TaskList />
      </Shell>
    ),
    errorElement: <ErrorPage />,
  },
  {
    path: '/submit',
    element: (
      <Shell>
        <Submit />
      </Shell>
    ),
    errorElement: <ErrorPage />,
  },
  {
    path: '/task/:taskId',
    element: (
      <Shell>
        <TaskDetail />
      </Shell>
    ),
    errorElement: <ErrorPage />,
  },
  {
    path: '/stats',
    element: (
      <Shell>
        <Stats />
      </Shell>
    ),
    errorElement: <ErrorPage />,
  },
  {
    path: '/intelligence',
    element: (
      <Shell>
        <IntelligenceHome />
      </Shell>
    ),
    errorElement: <ErrorPage />,
  },
  {
    path: '/intelligence/companies',
    element: (
      <Shell>
        <CompanyList />
      </Shell>
    ),
    errorElement: <ErrorPage />,
  },
  {
    path: '/intelligence/company/:companyId',
    element: (
      <Shell>
        <CompanyDetail />
      </Shell>
    ),
    errorElement: <ErrorPage />,
  },
  {
    path: '/intelligence/workbench',
    element: (
      <Shell>
        <AnalysisWorkbench />
      </Shell>
    ),
    errorElement: <ErrorPage />,
  },
  {
    // 兜底 404。放最后，匹配所有未声明路径。
    path: '*',
    element: (
      <Shell>
        <div className="rounded-lg border border-slate-200 bg-white p-6">
          <h2 className="text-lg font-semibold">页面不存在</h2>
          <Link to="/" className="mt-3 inline-block text-sm text-sky-700 hover:underline">
            返回任务列表
          </Link>
        </div>
      </Shell>
    ),
  },
])

export default function App() {
  return <RouterProvider router={router} />
}

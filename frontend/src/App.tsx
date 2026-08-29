import { createBrowserRouter, Link, RouterProvider, useRouteError } from 'react-router-dom'
import Console from './pages/Console'
import Overview from './pages/Overview'
import TaskList from './pages/TaskList'
import TaskDetail from './pages/TaskDetail'
import Stats from './pages/Stats'
import Submit from './pages/Submit'

/**
 * 顶部导航 + 内容区。所有页面共用，所以放在路由的 element 外层。
 *
 * `wide`：看板要一屏放完五个桶 + 两张表，6xl 会把表格挤到换行。查询类页面
 * 仍然用 6xl —— 正文行太长反而难读。
 */
function Shell({ children, wide = false }: { children: React.ReactNode; wide?: boolean }) {
  const width = wide ? 'max-w-[1600px]' : 'max-w-6xl'
  return (
    // 看板页整站切等宽：外壳和内容用两种字体时，导航栏和表格看起来像两个
    // 不同的应用拼在一起。查询页保持默认比例字体（那里有整段正文要读）。
    <div className={`min-h-screen bg-slate-50 text-slate-900 ${wide ? 'font-mono' : ''}`}>
      <header className="border-b border-slate-200 bg-white">
        <div className={`mx-auto flex ${width} items-center gap-6 px-6 py-4`}>
          <Link to="/" className="text-lg font-semibold tracking-tight">
            自动化无人工厂
          </Link>
          <nav className="flex gap-4 text-sm">
            <Link to="/" className="text-slate-600 hover:text-slate-900">
              看板
            </Link>
            <Link to="/overview" className="text-slate-600 hover:text-slate-900">
              闭环总览
            </Link>
            <Link to="/tasks" className="text-slate-600 hover:text-slate-900">
              任务
            </Link>
            <Link to="/submit" className="text-slate-600 hover:text-slate-900">
              投递
            </Link>
            <Link to="/stats" className="text-slate-600 hover:text-slate-900">
              统计
            </Link>
          </nav>
        </div>
      </header>
      <main className={`mx-auto ${width} px-6 py-6`}>{children}</main>
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
    // 首页给看板而不是任务列表：抬头就该知道现在是否健康，而不是先读一屏
    // 任务名。任务列表移到 /tasks，导航里还在。
    path: '/',
    element: (
      <Shell wide>
        <Console />
      </Shell>
    ),
    errorElement: <ErrorPage />,
  },
  {
    // 闭环总览也走 wide + 等宽：它是看板类页面（环形图 + 判据表要横向空间）。
    path: '/overview',
    element: (
      <Shell wide>
        <Overview />
      </Shell>
    ),
    errorElement: <ErrorPage />,
  },
  {
    path: '/tasks',
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

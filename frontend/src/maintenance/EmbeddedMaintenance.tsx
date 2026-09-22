import { useEffect } from 'react'
import { Route, Routes } from 'react-router-dom'

import type { PageProps } from '../workbench/ui'
import MaintenanceTaskDetail from '../workbench/MaintenanceTaskDetail'
import MaintenanceTasksPage from '../workbench/MaintenanceTasksPage'
import MaintenanceShell from './MaintenanceShell'
import ReposPage from './ReposPage'
import IntakePage from './IntakePage'
import AboutPage from './AboutPage'
import MonitorPage from './MonitorPage'
import { setMaintenanceApiBase } from './api'

/** Mount point for embedding the maintenance subsystem into another host,
 *  without the webuddy AppShell chrome. Same route shapes as the AppShell-
 *  nested version, just rooted at `basePath` instead of '/maintenance'. */
export default function EmbeddedMaintenance({ basePath, apiBase, ...pageProps }: PageProps & {
  basePath: string
  apiBase?: string
}) {
  useEffect(() => { if (apiBase) setMaintenanceApiBase(apiBase) }, [apiBase])

  return (
    <Routes>
      <Route element={<MaintenanceShell basePath={basePath} />}>
        <Route index element={<MonitorPage {...pageProps} />} />
        <Route path="repos" element={<ReposPage {...pageProps} />} />
        <Route path="repos/:projectId" element={<ReposPage {...pageProps} />} />
        <Route path="intake" element={<IntakePage {...pageProps} />} />
        <Route path="about" element={<AboutPage {...pageProps} />} />
        <Route path="tasks" element={<MaintenanceTasksPage {...pageProps} />} />
        <Route path=":taskId" element={<MaintenanceTaskDetail {...pageProps} />} />
      </Route>
    </Routes>
  )
}

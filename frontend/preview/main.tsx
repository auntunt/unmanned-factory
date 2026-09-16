import React from 'react'
import ReactDOM from 'react-dom/client'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import ConversationLayout from '../src/conversation/ConversationLayout'
import StartChat from '../src/conversation/StartChat'
import RunWorkspace from '../src/conversation/RunWorkspace'
import HistoryPage from '../src/conversation/HistoryPage'
import AgentCatalog from '../src/conversation/AgentCatalog'
import SettingsPage from '../src/conversation/SettingsPage'
import '../src/index.css'
import '../src/workbench/theme.css'

const route = new URLSearchParams(location.search).get('route') || '/'
const props = { csrfToken: 'x', onUnauthorized: () => {}, user: { id: 1, username: 'owner', role: 'admin' as const } }
const shell = { ...props, onLogout: () => {} }

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <MemoryRouter initialEntries={[route]}>
      <Routes>
        <Route element={<ConversationLayout {...shell} />}>
          <Route index element={<StartChat {...props} />} />
          <Route path="runs/:runId" element={<RunWorkspace {...props} pollMs={0} />} />
          <Route path="history" element={<HistoryPage {...props} />} />
          <Route path="agents" element={<AgentCatalog {...props} />} />
          <Route path="settings" element={<SettingsPage {...shell} />} />
        </Route>
      </Routes>
    </MemoryRouter>
  </React.StrictMode>,
)

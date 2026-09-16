import React from 'react'
import ReactDOM from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { RoutedWorkbench } from '../src/App'
import '../src/index.css'
import '../src/workbench/theme.css'

// Screenshot links keep ?route= support, but share the production route tree.
const route = new URLSearchParams(location.search).get('route') || '/'
const session = { csrf_token: 'preview', user: { id: 1, username: 'owner', role: 'admin' as const } }
const logout = () => window.location.assign('/')
ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <MemoryRouter initialEntries={[route]}>
      <RoutedWorkbench session={session} logout={logout} />
    </MemoryRouter>
  </React.StrictMode>,
)

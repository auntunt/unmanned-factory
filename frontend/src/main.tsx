import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './index.css'
import './workbench/theme.css'
import { applyTheme, storedTheme } from './workbench/ThemeSwitch'
applyTheme(storedTheme())

const rootEl = document.getElementById('root')
if (!rootEl) {
  throw new Error('找不到 #root 挂载节点')
}

ReactDOM.createRoot(rootEl).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)

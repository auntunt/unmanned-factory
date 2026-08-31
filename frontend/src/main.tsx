import React from 'react'
import ReactDOM from 'react-dom/client'
import { ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import App from './App'
import { C, FONT } from './theme/tokens'
import './index.css'

const rootEl = document.getElementById('root')
if (!rootEl) {
  throw new Error('找不到 #root 挂载节点')
}

ReactDOM.createRoot(rootEl).render(
  <React.StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          colorPrimary: C.primary,
          colorSuccess: C.success,
          colorWarning: C.warning,
          colorError: C.error,
          colorText: C.text,
          colorTextSecondary: C.textSub,
          colorBorder: C.border,
          colorBorderSecondary: C.borderLight,
          fontFamily: FONT,
          borderRadius: 6,
        },
      }}
    >
      <App />
    </ConfigProvider>
  </React.StrictMode>,
)

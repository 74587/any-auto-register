// antd 5 的静态方法（message / notification / Modal.confirm）在 React 19 下走的是
// 已被移除的 ReactDOM.render，不打这个补丁全站的提示都会静默失败。
import '@ant-design/v5-patch-for-react-19'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)

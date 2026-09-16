# 对话工作台界面预览

在 `frontend` 目录运行：

```sh
npm exec vite -- --config vite.preview.config.ts --host 127.0.0.1 --port 5200 --strictPort
```

打开 http://localhost:5200/。它复用正式应用入口，菜单页面使用只读示例数据。
历史作品中可查看制作中、暂停和成果交付三种状态；成果卡支持静态预览。

截图入口仍支持 `/preview/?route=/runs/delivered`，与正式入口共用路由树。
所有页面顶端均标明预览环境。写入、上传、探针和真实下载不会执行；
缺少夹具的接口会明确提示，不会用空对象伪装成功。

正式运行请使用普通开发配置连接后端，或构建后由真实服务提供页面。
`preview/` 与此 Vite 配置不参与生产构建。

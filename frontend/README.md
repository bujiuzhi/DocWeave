# DocWeave 前端

DocWeave 的 Vue 3、TypeScript 与 Vite 单页应用，通过同源 `/api/v1/` 接口访问后端。

## 本地开发

```bash
pnpm install --frozen-lockfile
pnpm dev
```

开发服务器默认把 `/api` 代理到 `http://127.0.0.1:8000`。需要连接其他本地后端时，可在启动前设置 `VITE_API_PROXY_TARGET`。该配置只用于开发代理；LLM 服务地址与 API Key 仍由后端环境变量管理，不应写入前端代码或浏览器存储。

## 验证

```bash
pnpm build
pnpm audit --prod
```

# 电力现货价格工作台（托管版）

基于 Vinext、Cloudflare Workers 和 D1 的私有网站版本。每位登录用户拥有独立配置与数据空间。

## 功能

- 加密保存采集 Token 和大模型 API Key，浏览器端只接收脱敏状态。
- 自定义 1—366 天，按地区并发采集并显示进度。
- 全国日均实时价格曲线、地区周期均价和原始分时时点价格区间占比。
- 区间分布图导出 PNG。
- DeepSeek、智谱 GLM 或 OpenAI 兼容接口的周期总结。

## 本地开发

```bash
npm install
npm run dev
```

本地 D1 使用项目内的 Miniflare 状态。生产环境必须配置 `APP_ENCRYPTION_KEY`，并绑定名为 `DB` 的 D1 数据库。

## 验证

```bash
npm run lint
npm test
```

完整 Python 采集、CSV/Excel/HTML 导出和 Docker 入口位于父目录；托管版针对浏览器操作和私有数据空间进行了独立实现。

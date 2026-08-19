# dljy-quant MCP（已退役 · 2026-08-03）

本胶水 MCP 已完成历史使命并删除。dljy 的量化/预测/复盘能力现在由
**dljy 仓内官方对外 MCP 服务**提供（物理位置 `dljy/packages/dljy-mcp/`）：

```bash
openclaw mcp add dljy \
  --command <dljy>/integrations/agent-backend/.venv/bin/python \
  --arg=-m --arg=dljy_mcp --arg=--profile --arg=trading \
  --cwd <dljy>/integrations/agent-backend
```

- 14 个 trading 档技能（预测 5 + 行情复盘 5 + 报价结算 3 + 数据资产 1）
- kelly/cvar/twap 等通用金融工具箱按用户决策收入 `--profile experimental`（默认不暴露）
- 战役记录：ChenShi-Tech/dljy Epic #1588；文档 `docs/agent-harness/dljy-mcp-service.md`

同目录 `power_price_mcp.py`（真实电价查询，数据到今天）**继续在役**，与 dljy-mcp 分工不变。

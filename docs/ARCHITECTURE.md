# 架构分层

与 `account_manager` 对齐的扁平包结构：

```
controller/  →  HTTP 路由（FastAPI APIRouter）
service/     →  业务逻辑（禁止 import FastAPI）
dao/         →  SQLite 访问
model/       →  实体与 Web DTO
config/      →  协议常量
common/      →  HTTP session、MSA API 客户端
frontend/    →  静态 Web UI
```

## 依赖方向

- `controller → service → dao` 单向
- `service/web/runtime.py` 承载 Job、SSE、账号导入导出等 Web 共享逻辑
- `main.py` 唯一根入口：无参数时 `uvicorn main:app`；带 CLI 参数时走 `service/registration/cli.py`
- 其它 CLI 工具在 `scripts/`（如 `check_imap.py`、`exchange_code.py`）

## API 路径

现有 `/api/*` 路径保持不变，前端 `frontend/static/index.html` 无需改 fetch URL。

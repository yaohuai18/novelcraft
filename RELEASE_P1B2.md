---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: e64d0ca98a0a22abfccfa0f49e721f06_f62d270ac64711f19b82525400de85a5
    ReservedCode1: lDly8OXtfTc20JnI/WrdRkpx0qGp/4NaXAY8TP2JVbgNlr/DwP7JJBfwX7WT9XuOCSR6EJIABf/VAU+Kv1UkKVB4cOzpqsViv8VO/4HCOAhH/ztjDcaIkogqEYxHHjv5oa873dDVUXcr9/M37Vkb8ucN53PhA/MSdlavc3k7v7q7TeX0C82TFOaEtj4=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: e64d0ca98a0a22abfccfa0f49e721f06_f62d270ac64711f19b82525400de85a5
    ReservedCode2: lDly8OXtfTc20JnI/WrdRkpx0qGp/4NaXAY8TP2JVbgNlr/DwP7JJBfwX7WT9XuOCSR6EJIABf/VAU+Kv1UkKVB4cOzpqsViv8VO/4HCOAhH/ztjDcaIkogqEYxHHjv5oa873dDVUXcr9/M37Vkb8ucN53PhA/MSdlavc3k7v7q7TeX0C82TFOaEtj4=
---

# NovelCraft（Marvis 版）P1 第二批发布记录

## 版本固化

- 分支：`master`
- Commit：`2af6652148c5c4bd049a3fafc42dd6e20cedd46d`
- 标题：`P1第二批: 审核中心/协作权限/操作审计三页 + 实验接口访问控制 + 任务中心/一致性/建议统一接入`
- 变更规模：31 个文件，+2888 / -92
- 工作区状态：干净（`git status` 无未提交改动）

## 验收测试脚本定位

- 路径：`backend/temp/test_p1_batch2_e2e.py`
- 性质：一次性验收脚本，走「内存 SQLite + httpx ASGITransport」范式（不连真实 8000 进程）
- 版本库策略：`backend/temp` 已被 .gitignore 忽略，脚本不入库
- 处置：保留为一次性验收脚本；如需长期回归，应迁移到正式测试目录并纳入测试命令（后续独立任务）

## 发布后冒烟验收

- E2E 复跑：`44/44 PASS`
- 真实后端（127.0.0.1:8000）健康：`/health` 200，`source_hash=2af6...`（与当前 commit 一致）
- 未登录核心接口 `/api/projects` → 403；未登录实验接口 `/api/v1/projects` → 403
- OpenAPI：总 path 435 个，实验操作 105 个（符合预期）
- 审核流程 reviewing→needs_changes→re_reviewing→approved 全链路通过

## 权限边界结论

已通过（E2E 覆盖）：
- 被移除成员旧请求立即失效（403）
- editor / reviewer / admin / owner 操作范围区分
- 邀请未接受用户不获得组织权限；重复/撤销邀请被拒
- 组织不存在 404、非成员访问 403，符合信息暴露策略
- 审计日志普通成员越权读取返回 403
- `require_experimental_access` 仅挂载 11 个实验 router + 根路由 `/`，核心/管理接口无误伤

## 已知风险（待决策）

- `.env` 中 `DEBUG=true`，`require_experimental_access` 在 debug 模式下直接放行，导致普通登录用户可访问实验接口（实测 `/api/v1/projects` 返回 200）。
- 若需生产收紧，应改 `DEBUG=false` 并重启后端进程 21372。

## 生产运行状态

- 后端：`python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`，工作目录 `backend`，PID 21372，Python 3.11.8
- 前端：`node vite.js`，工作目录 `frontend`，PID 16784，127.0.0.1:5173
- 环境变量来源：`backend/.env`（键：APP_NAME / DEBUG / DATABASE_URL / JWT_SECRET_KEY / JWT_ALGORITHM / ACCESS_TOKEN_EXPIRE_MINUTES / DEEPSEEK_API_KEY / AI_BASE_URL / AI_MODEL 等）
- 数据库：`backend/novelcraft.db`（约 21.4 MB）
- 数据库备份：`backend/novelcraft.db.bak-20261011-222756`、`backend/novelcraft.db.bak-20261011-233647`（本次发布前未新增备份，建议冻结前补一次）

## 回滚步骤

```powershell
# 回退到本 commit（当前即为该版本）
git checkout 2af6652148c5c4bd049a3fafc42dd6e20cedd46d
# 回退到 P1 第二批之前
git checkout 2b0870b
# 若需丢弃本地未提交改动（谨慎）
git reset --hard <commit>
```

## 冻结声明

P1 正式关闭，暂停功能扩张。后续拆独立任务：P1 收尾（统一响应结构/错误码/分页/API 文档）、大纲生成异步化、P2 企业协作、失败样本注入。
*（内容由AI生成，仅供参考）*

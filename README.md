---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: e64d0ca98a0a22abfccfa0f49e721f06_aa7b587bc13911f1887c525400de85a5
    ReservedCode1: tfKIDjfWTF156B9zHJnjeTkc3ybgHP6OEEUI7ImbiAnB9e5RdvvgHrZVbWCv5CPch/EotUfX6gMOp4Aghc6JljbN7Fn/cVQc4HMKVE2rVixBI4VedVivmfbQPPiGSBYFxaz7yDb3POfMIb2DacfGKCjvQaQrSTTlY2d1Q4W0GVbMQZVCUVKS8ZutI9Q=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: e64d0ca98a0a22abfccfa0f49e721f06_aa7b587bc13911f1887c525400de85a5
    ReservedCode2: tfKIDjfWTF156B9zHJnjeTkc3ybgHP6OEEUI7ImbiAnB9e5RdvvgHrZVbWCv5CPch/EotUfX6gMOp4Aghc6JljbN7Fn/cVQc4HMKVE2rVixBI4VedVivmfbQPPiGSBYFxaz7yDb3POfMIb2DacfGKCjvQaQrSTTlY2d1Q4W0GVbMQZVCUVKS8ZutI9Q=
---

# NovelCraft

AI 长篇小说创作平台：前后端分离（FastAPI + SQLite + React/Vite）。支持 **演示模式（mock）** 与 **AI 真实模式（real）** 两种模式。

## 功能

- 项目与大纲：作品库列表（`/projects`）、项目详情与作品设定（`/projects/:projectId`）、分层大纲
- 章节编辑：章节正文与版本、AI 续写/改写/润色、结果接受前保存与版本冲突保护
- 设定与资料：人物、人物关系、世界观、时间线、伏笔
- 质量与任务：一致性校验、AI 任务中心（异步生成、取消与重试）
- 发布与协作：发布页、审核中心、协作、审计日志、个人设置
- 创作页（`/create`）：演示模式 / AI 真实模式切换，草稿保存在 localStorage
- 演示模式：未配置密钥时启用，生成内容标注「演示生成」，且随设定动态变化
- AI 模式：配置 DEEPSEEK_API_KEY 后启用，真实调用 DeepSeek（OpenAI 兼容 Chat Completions），返回 `mode=real`
- API Key 只存于服务端环境变量，绝不写入前端 / localStorage

## 技术栈与质量体系

- 技术栈：Python 3.11 · FastAPI · SQLAlchemy async · Pydantic 2 · SQLite（WAL 模式）｜ React 18 · Vite 5 · TypeScript
- 4 维一致性快速校验：伏笔顺序 / 时间线（支持中文年号）/ 人物状态（死亡后行动）/ 规则关键词，保存前自动体检
- 追读力报告：章末钩子覆盖率、爽点密度、伏笔债务（规则版计算，零 AI 成本）
- 失败样本自动沉淀：AI 改写后与原文差异过大时入库，供提示词迭代
- 全文检索：SQLite FTS5 + 中文 bigram 索引（1-3 字词条短语查询）
- 数据安全：SQLite WAL、每日自动备份 + 月度恢复演练、迁移网关（SCHEMA_VERSION 版本化）
- 测试：pytest 810 用例基线（`TEMP` 指向可写目录后 `python -m pytest tests/ -q`）


## 目录结构

```
NovelCraft/
├── backend/            # FastAPI 后端
│   ├── .env            # 本地环境变量（含真实密钥，勿提交）
│   ├── .env.example    # 变量模板（不含真实密钥）
│   ├── requirements.txt / requirements.lock.txt
│   ├── sql/            # 建表 / 迁移 SQL
│   ├── tests/          # pytest 用例
│   └── app/
│       ├── core/       # 配置、数据库连接、鉴权
│       ├── models/     # SQLAlchemy ORM 表定义
│       ├── schemas/    # Pydantic 请求/响应模型
│       ├── routers/    # API 路由（约 60 个文件）
│       ├── services/   # 业务逻辑（约 111 个文件）
│       ├── utils/
│       └── main.py     # FastAPI 应用入口
└── frontend/           # React + Vite 前端
    └── src/
        ├── api/client.ts
        └── pages/      # 17 个页面（含 AI 创作页 CreatePage）
```

## 配置（后端）

1. 进入 `backend/`，复制模板：

   - Windows：`copy .env.example .env`
   - macOS / Linux：`cp .env.example .env`

2. 编辑 `.env`，填入真实密钥：

   ```
   DEEPSEEK_API_KEY=sk-你的密钥
   AI_BASE_URL=https://api.deepseek.com
   AI_MODEL=deepseek-chat
   ```

3. 密钥优先级：`DEEPSEEK_API_KEY` > `AI_API_KEY` > `OPENAI_API_KEY`；模型名与地址读 `AI_BASE_URL` / `AI_MODEL`（缺省 `https://api.deepseek.com` / `deepseek-chat`，URL 兼容带不带 `/v1` 后缀）。
4. 不要填写占位值（`sk-demo-*` / `your-*` / `replace-*`），否则会被判定为未配置而走演示模式。

## 启动

后端（在 `backend/` 下，必须使用 venv 解释器）：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

前端（在 `frontend/` 下）：

```powershell
npm.cmd run dev   # http://localhost:5173
```

也可以双击项目根目录的 `autostart_backend.bat` / `autostart_frontend.bat` 启动，日志写在 `autostart_logs\`。

- 后端 API 文档：http://127.0.0.1:8000/docs
- 仓库内未检索到默认演示账号的种子代码，账号创建方式待确认；文档不提供默认凭据。

## 如何在页面切换到 AI 模式

1. 打开创作页 http://localhost:5173
2. 右上角「模式」开关：切到 **AI 模式** 后显示当前真实配置（模型 / 地址）；
3. 若显示「尚未配置 AI 服务」+ 配置说明，说明后端未检测到有效密钥，请按上文配置 `.env` 并重启后端；
4. AI 模式未配置时按钮可点但不会伪装成功，会返回结构化错误提示。

## 如何确认 AI 模式真实生效

- **页面**：生成后正文上方显示 `mode=real`，且两次生成结果随设定不同（真实模型输出，无「演示生成」标注）；
- **接口**：直接调用后端（需先登录拿 token）：

  ```bash
  # 登录
  curl -X POST http://localhost:8000/api/auth/login \
    -H "Content-Type: application/json" \
    -d '{"username":"<你的用户名>","password":"<你的密码>"}'
  # 用返回的 access_token 调用 AI 状态接口
  curl http://localhost:8000/api/ai/status -H "Authorization: Bearer <token>"
  # 期望：{"configured":true,"mode":"real","base_url":"https://api.deepseek.com","model":"deepseek-chat"}
  ```

  生成接口返回体含 `"mode": "real"` 即真实模式生效。

## 主要接口

| 接口 | 说明 |
|---|---|
| `GET /api/ai/status` | 当前模式与配置（不含密钥） |
| `POST /api/ai/generate-outline-first` | 生成「全书大纲 + 第一章」 |
| `POST /api/ai/generate-next-chapter` | 生成下一章（携带大纲/前情/人物状态） |
| `POST /api/ai/regenerate-chapter` | 重新生成本章 |
| `POST /api/ai/expand-chapter` | 扩写本章 |
| `POST /api/ai/polish-chapter` | 润色本章 |
| `POST /api/ai/generate-novel` | 整本生成（非流式） |
| `POST /api/ai/generate-novel/stream` | 整本生成（SSE 流式） |

所有 AI 接口带每用户限流（超出返回 429 可读错误），调用失败返回结构化中文错误（`{"detail":{"code":"...","message":"..."}}`）。

实验接口（如 `/api/openapi/*`、`/api/billing/*`、`/api/analytics/*`）受开关控制：默认仅平台管理员可访问，普通用户需在开发模式（`debug=True` 且显式开启 `experimental_open_in_debug`）下才能调用。
*（内容由AI生成，仅供参考）*

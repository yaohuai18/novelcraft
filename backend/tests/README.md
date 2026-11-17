# backend/tests —— 阶段 1 正式回归测试（离线）

阶段 1 冻结期新增目录：把原先散落在 `backend/temp/`（被根 `.gitignore` 的 `temp/` 规则忽略、
无 pytest 配置、不随提交追踪）的关键回归脚本，迁入本目录成为**正式可追踪**的测试资产。

## 运行方式

```powershell
cd backend
python -m pytest              # 依据 backend/pytest.ini，自动只跑 tests/
python -m pytest tests -q     # 等价写法
```

## 迁移清单（源脚本 → 正式用例，内容等价）

| 正式用例 | 来源（backend/temp/） | 覆盖要点 |
|---|---|---|
| `test_phase1_model_policy.py` | `test_model_policy_phase1.py` | 模型策略表/任务分流矩阵、legacy 别名只映射不派发、思考任务掉温、请求体字段封闭、未知 task_type 告警回退、summary 隔离 |
| `test_phase1_generation_consistency.py` | `test_gen_consistency.py` | 不静默截断、over_target/incomplete 状态、raw_content 恒为原始响应、流式拼接、迁移幂等补列 |
| `test_phase1_entity_reference.py` | `test_phase_e_entity_reference.py` | 实体引用校验（拒绝跨项目/非法 UUID、strict 开关）、写路径拒绝时不落库 |
| `test_phase1_token_accounting.py` | 新增（阶段 1 冻结期） | token 口径：未知 usage 不伪造、日聚合不污染、读取侧 cost_billable=False、上报 schema 允许 NULL、record_generation 无 usage 时四字段全 NULL |
| `test_phase1_continuation_regression.py` | 新增（阶段 1 冻结期） | 续写链路：take_last_ai_meta/usage 取一次即清空、正文不含 reasoning_content、修复依赖链可导入 |

## 离线与安全约束

- `conftest.py` 提供 autouse 离线护栏：真实 `socket.connect` / `httpx.AsyncClient.send` 直接失败；
- AI 调用统一 `monkeypatch httpx.AsyncClient.post` 注入假响应，使用假密钥 `sk-test-phase1-offline`；
- 数据库一律 `sqlite+aiosqlite` 内存库（`StaticPool`），不触碰 `backend/novelcraft.db`；
- 不使用真实历史正文与真实线上响应样本；只做只读校验，不执行迁移、不写业务数据。

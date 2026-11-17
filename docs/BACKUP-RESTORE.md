# NovelCraft 数据库快照与恢复手册

> 适用仓库：`NovelCraft`（Marvis 版）
> 工具：`scripts/db-snapshot.py`
> 本文件只写流程与安全边界，不包含任何凭据、连接串或数据库内容。

---

## 1. 在线快照的用途

`db-snapshot.py` 用 Python 标准库 `sqlite3.Connection.backup` 在**服务不停机**的情况下，
从运行中的 SQLite 正式库生成一份**一致**的快照副本。

适用场景：

- 大版本发布、批量迁移、危险脚本执行前的回退点；
- 需要一份可长期保存、可校验的历史数据副本；
- 为“快照完整性检查”提供固定对象（区别于运行库漂移报警）。

它**不是**项目级 JSON 备份（`project_backups` 表）的替代：
后者是应用内的章节/设定快照，本身仍存放在同一个数据库里，库文件损坏时一起丢失。

**本工具只负责创建和校验快照，没有恢复命令。** 恢复是按本文档手工执行的流程（见第 5–8 节）。

## 2. 快照不会覆盖正式库

- 源库一律以 `mode=ro`（只读 URI）打开；
- 工具只读源库、只写目标目录，**不会修改、移动或删除正式库**；
- 目标目录/文件已存在时直接失败（退出码 4），绝不覆盖；
- 先写临时文件、校验通过后再原子重命名；失败只清理本次新建的临时文件与半个快照。

## 3. 快照输出必须在仓库外

- 默认输出根目录：`%USERPROFILE%\NovelCraft-backups\database`；
- 输出目录若位于仓库内，工具直接拒绝（退出码 3）；
- 源数据库必须位于仓库根目录内；仓库外或 UNC/远程路径直接拒绝（退出码 3）；
- 快照是**完整业务数据库**，可能包含用户账号数据、创作正文和其他业务数据；
- **不是脱敏副本**：它不裁剪、不脱敏、不脱敏化字段，不能当作可对外分享的样本；
- 快照目录必须限制访问（仅本机当前用户），**禁止外发、上传或提交 Git**。

## 4. 创建快照前必须单独授权

即使工具本身是只读源库，创建快照仍属于“读正式数据 + 在仓库外落盘”。
因此每次真实执行前都需要明确授权，至少说明：

- 输出根目录；
- 是否带 `--label`；
- 预估快照大小（正式库当前约 150 MB 量级）；
- 磁盘剩余空间。

运行前置条件（不满足直接 exit 3，不会生成快照）：

- 必须能确定 Git HEAD：在 Git 仓库内运行，或显式传 `--head <提交号>`；
- 源数据库必须位于仓库根目录内，且不能在快照目录内；
- 输出目录必须在仓库外，且不能是 UNC/远程路径。

`--dry-run` 可用于事前检查：它只校验路径与参数，**不读取数据库、不创建目录**。

```powershell
# 只检查，不执行
backend\.venv\Scripts\python.exe scripts\db-snapshot.py `
  --database backend\novelcraft.db --dry-run

# 真正创建（需授权）
backend\.venv\Scripts\python.exe scripts\db-snapshot.py `
  --database backend\novelcraft.db --label "发布前"
```

## 5. 恢复必须停止后端

恢复会替换正在使用的数据库文件，必须：

1. 停止后端（8000）并确认端口不再监听；
2. 确认没有测试/脚本/定时任务仍在访问该库；
3. 前端可保持运行，但所有 `/api` 请求会失败，属预期。

**禁止**在服务运行中替换 `.db` 文件。

## 6. 恢复前必须先做 pre-restore 快照

在动手替换之前，先用当前库再做一次快照（或人工复制三件套），
确保“恢复动作本身”也可回退。

pre-restore 的识别依据有两层，必须同时核对：

1. 目录名符合 `novelcraft-db-YYYYMMDD-HHmmss-<7位小写十六进制>`；
2. `manifest.json` 的 `label` 以 `pre-restore-` 开头（例如 `pre-restore-20261101-1930`）。

`label` 是人工填写的自由文本，**不能单独作为安全判据**；目录名与 manifest 字段都要校验。
pre-restore 快照可以与正式库同盘，但同盘**不能防御磁盘损坏**，异地/异盘备份目前仍未实现。

## 7. 恢复时同时处理旧的 `-wal` / `-shm` / `-journal`

SQLite 在 WAL 模式下会留下 `-wal` / `-shm`；回滚模式异常退出还可能留下 `-journal`。
用快照替换主库时：

- 必须同时处理旧 `novelcraft.db-wal`、`novelcraft.db-shm` 与可能存在的 `novelcraft.db-journal`；
- **只改名隔离，不直接删除**：改名为带 `.stale-<operation_id>` 后缀的文件（`operation_id` 在恢复开始前一次性生成，见第 16.11 节第 2 条）；等恢复成功并通过第 17 节检查后，再决定是否删除；
- 否则 SQLite 可能回放旧 WAL、报错或拒绝打开；
- 处理前确认后端已停止、无进程持有文件。

**试开与边车的顺序（必须遵守）**：

1. 独占试开（第 16.1 节第 3 步）可能由 SQLite 在库旁创建或刷新 `-wal`/`-shm`；
2. 试开结束后必须**重新扫描**正式库目录，记录全部相关边车；
3. 任何由试开产生的边车，都必须纳入第 16.1 节第 8 步的隔离清单；
4. 不在真实快照目录中打开数据库；隔离副本统一 `mode=ro`，不使用 `immutable=1`（见 15.2）。

## 8. 恢复后启动服务并执行健康检查

```powershell
# 启动
autostart_backend.bat

# 健康检查（应全部 200；完整清单见第 17 节）
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/health/ready
```

业务接口验证**不属于默认恢复后检查**，必须另行授权；默认只执行健康端点与非业务元数据检查（PID、监听、`source_hash`、`schema_version`），不读取章节、用户或任何业务内容。

## 9. 不得把数据库快照提交 Git

- `.gitignore` 已覆盖 `*.db`、`*.db-wal`、`*.db-shm`、`*.db.bak-*`；
- 快照目录默认在仓库外，不应出现在 `git status`；
- 任何时候都不要 `git add -f` 快照文件。

## 10. 不删除旧快照，除非另行授权

- 保留策略：最近 10 个数据库快照、最长 180 天（人工维护）；
- 删除属于不可逆操作，必须先列清单、再单独授权；
- 工具本身**没有**删除功能。

## 11. 代码 bundle 与数据库快照不是一回事

| 类型 | 保护对象 | 工具 |
|---|---|---|
| Git bundle | 代码历史 | `git bundle create` |
| 数据库快照 | 业务数据 | `scripts/db-snapshot.py` |
| 项目级 JSON 备份 | 单个项目的章节/设定 | 应用内备份接口 |

三者互补，不能互相替代。数据库快照无法恢复代码，bundle 也无法恢复数据。

## 12. 不要把运行库的冻结指纹测试当作快照完整性测试

- `formal_db`（`test_i3a_fix_migration_gateway.py`）针对的是**运行库**的冻结基线，
  运行库每次业务写入都会让它失败，已用 marker 隔离、默认不跑；
- 快照完整性应由未来的 `snapshot_integrity` 类测试针对**固定快照文件**校验，
  两者不要混在同一个红灯里。

---

## 13. 工具能力边界

`db-snapshot.py` 目前只做三件事：

1. 用在线 backup API 创建一份快照；
2. 对快照做 `integrity_check` / `quick_check`；
3. 写一份不含敏感字段的 `manifest.json`。

它**不做**：恢复、删除旧快照、自动保留策略、上传/同步、脱敏、导出业务内容。
恢复必须按本文档手工执行；删除必须另行授权。

**当前没有正式恢复脚本。** `scripts/` 下与备份相关的脚本是：

| 脚本 | 职责 | 是否接触正式库 |
|---|---|---|
| `db-snapshot.py` | 创建快照、完整性检查、写 manifest | 只读打开正式库 |
| `report-db-snapshots.py` | 只读报告快照清单与分类建议 | 不打开任何数据库 |
| `drill-db-restore.py` | 把**一份快照副本**复制到 TEMP 并做只读完整性检查 | 不接触正式库 |

三者都不具备恢复、替换或删除数据库的能力。正式恢复仍是手工、高风险、需要单独授权的操作。

---

## 附：退出码

| 退出码 | 含义 |
|---|---|
| 0 | 成功 / dry-run 通过 |
| 2 | 参数错误（缺 `--database` 等） |
| 3 | 校验失败（HEAD 不可确定、源不在仓库内、UNC/远程路径、输出在仓库内、非 SQLite 等） |
| 4 | 目标已存在（拒绝覆盖） |
| 5 | 备份失败（含数据库忙/被锁定） |
| 6 | 完整性检查失败 |

---

## 14. 快照保留与人工清理

保留策略以“只报告、人工删除”为默认，不实现自动清理。

### 14.1 报告工具

`scripts/report-db-snapshots.py` 只读快照目录的文件系统元数据与 `manifest.json` 的允许字段，
输出 `KEEP` / `REVIEW` / `DELETE_CANDIDATE` 三类建议：

```powershell
<repo>\backend\.venv\Scripts\python.exe scripts\report-db-snapshots.py --root "%USERPROFILE%\NovelCraft-backups\database"
<repo>\backend\.venv\Scripts\python.exe scripts\report-db-snapshots.py --json
```

- `--keep 10`、`--max-age-days 180` 只是分类阈值，**不是删除规则**；
- 报告没有 `--delete` / `--remove` / `--clean`，也没有任何删除、移动、复制、重命名或写入快照目录的代码路径；
- 不打开 SQLite 文件，不重跑 `integrity_check` / `quick_check`，不计算哈希；
- 不跟随符号链接、junction 或 reparse point，不递归扫描；
- `manifest.json` 只投影允许字段，`backup_sha256` 与未知字段不读取、不输出。

### 14.2 分类规则

| 分类 | 含义 |
|---|---|
| `KEEP` | 最近 N 个，或年龄不超过 `--max-age-days`，且契约完整 |
| `REVIEW` | manifest 缺失/损坏、字段类型错误、快照文件缺失、大小不一致、完整性非 ok、存在额外文件或临时边车、`created_at` 不可解析 |
| `DELETE_CANDIDATE` | 同时满足：不在最近 N 个、年龄超过阈值、契约完整；**仅表示可人工复核，未授权删除** |

快照总数不超过 `--keep` 时，任何项都不会被列为 `DELETE_CANDIDATE`。

### 14.3 人工清理边界

1. 报告只读，不自动删除任何快照；
2. `DELETE_CANDIDATE` 必须另行授权，逐个确认；
3. 删除只允许使用**精确快照目录**，禁止通配符、递归批量或按扩展名清理；
4. 删除前核对：目录名符合 `novelcraft-db-YYYYMMDD-HHmmss-<7位hex>`、父目录是快照根、`manifest.json` 可解析、删除后仍至少保留一个可用快照；
5. 不删除最后一个可用快照，不因磁盘空间压力自动清理；
6. 不处理正式数据库、仓库内历史备份或日志；
7. 快照是完整业务库，可能含账号数据与创作正文，禁止上传、外发或提交 Git；
8. 快照完整性状态来自创建时的 manifest；本报告不重新打开数据库验证。

### 14.4 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 报告成功（存在 `REVIEW` 或 `DELETE_CANDIDATE` 也是 0） |
| 2 | 参数错误、`--root` 指向文件、UNC/远程路径或无法安全扫描 |

---

## 15. 隔离恢复演练

正式替换数据库前，先在隔离目录里验证“快照本身能不能用”。这一步不接触正式库、不停服务。

### 15.1 已完成的演练结果（2026-11-01 实测）

- 演练只在本机 `%TEMP%\nc-restore-drill-<时间戳>-<随机后缀>\` 下进行；
- 只复制快照中的 `novelcraft.db`，**不复制** `manifest.json`、`-wal`、`-shm`、`-journal`；
- 副本大小与 `manifest.snapshot_size` 一致（154,058,752 B）；
- 用 SQLite URI `file:<path>?mode=ro` 成功打开，`PRAGMA integrity_check` 与 `PRAGMA quick_check` 均为 `ok`；
- 打开期间 SQLite 在**副本旁**生成了 `novelcraft.db-shm` 与 `novelcraft.db-wal`（见 15.2）；
- 演练目录按路径守卫精确删除，源快照与正式库未被改动。

演练所用快照来源 HEAD 为 `e7ed8fb`，当时代码 HEAD 为 `9dd6f2f`，相差 3 个提交。
因此该次 `ok` 只说明**这份快照文件结构完整**，不说明当前代码能在它上面正常启动。

### 15.2 WAL 边车行为（重要）

WAL 模式的数据库即使以 `mode=ro` 打开，SQLite 也可能在副本旁创建：

- `novelcraft.db-shm`
- `novelcraft.db-wal`

由此得出四条操作规则：

1. 这些边车**不是**正式快照的一部分，也不属于 `manifest.json` 记录的产物；
2. **不要直接在真实快照目录内执行完整性检查**——会污染快照目录；
3. 必须先复制到隔离目录，再在隔离副本上校验；
4. 校验结束后先关闭连接，再按路径守卫清理演练目录。

另外，本项目实测：对 WAL 快照使用 `immutable=1` 会报 `unable to open database file`，
所以 **`immutable=1` 不得作为本项目快照的默认校验方式**。

### 15.3 演练步骤

1. 在本机 `%TEMP%` 下创建唯一目录，名称以 `nc-restore-drill-` 开头；
2. 只复制目标快照的 `novelcraft.db` 到 `<演练>\restored\novelcraft.db`；
3. 比对副本大小与 `manifest.snapshot_size`，不一致立即停止；
4. 用 `file:<副本路径>?mode=ro` 打开，只执行 `PRAGMA integrity_check` 与 `PRAGMA quick_check`；
5. 不执行 `SELECT`，不读取表、行、字段或业务数据，不连接运行中的服务；
6. 关闭连接后，确认演练目录内只有本次副本及 SQLite 产生的边车；
7. 只删除本次新建的演练目录；失败时保留现场并报告路径。

### 15.4 演练能证明与不能证明什么

- **能证明**：快照文件可被 SQLite 打开、页结构通过完整性检查、大小与 manifest 一致。
- **不能证明**：应用能否在该库上成功启动；业务数据是否正确；迁移是否安全。
- 不要把 `integrity_check=ok` 当成业务数据正确性证明。

---

## 16. 正式恢复：编号化步骤、同卷规则与硬守卫

> 本节是**设计稿**，不是可执行脚本。任何一步失败都必须停止、保留现场，不得跳步或重试第二步。

### 16.1 编号化替换步骤

0. 前置：本次恢复已获单独授权，记录操作者、授权信息、时间、源快照目录；按第 16.9 节第 14 条核对磁盘剩余空间（同卷 ≥4× 正式库大小；pre-restore 在另一卷时执行卷 ≥3×、该卷 ≥1×），不足即停止。
1. **停止后端**：关闭 8000，`Get-NetTCPConnection -LocalPort 8000 -State Listen` 必须为空；确认由 `autostart_backend.bat` 拉起的 cmd、python、uvicorn 进程均已退出。
2. **确认无其他访问者**：没有 pytest、快照脚本、演练脚本、定时任务、SQLite 工具或编辑器打开正式库；无法排除时停止。
3. **独占试开与句柄边界检查**：对 `backend/novelcraft.db` 做一次“打开即关”的独占尝试（不读内容）；成功后立即重新扫描正式库目录并记录全部边车（见第 7 节）。试开只作为“无进程持有”的近似证据，不宣称能枚举 Windows 上的全部句柄。
4. **创建并核对 pre-restore 快照**：按第 6 节命名与 `label` 规则创建；核对 `snapshot_size`、`integrity_check`、`quick_check`；不得覆盖或删除既有快照。
5. **校验源快照、manifest、大小与 staging 副本**：核对目录名与 manifest 字段（`snapshot_file`、`snapshot_size`、`method`、`integrity_check`、`quick_check`）；**真实快照只作为源**：先记录源的大小/mtime/SHA-256，复制到专用 staging 目录后再次记录并比对（见第 16.9 节第 2–6 条），再在 staging 副本上以 `mode=ro` 跑两个 PRAGMA；不使用 `immutable=1`；不在真实快照目录内打开数据库；**不得用 `drill-db-restore.py` 充当本条流程**（它成功后删除演练目录，见第 19 节）。
6. **检查正式目标及父路径**：目标必须解析到 `backend/novelcraft.db`；目标与每一级父路径不得为 symlink、junction 或其他 reparse point（用 `abspath` + `lstat` + reparse 属性检查，且必须先于任何替换、打开或目录操作）；临时落位目录必须是正式库所在目录。
7. **旧主库改名隔离**：`novelcraft.db` → `novelcraft.db.stale-<operation_id>`；改名前确认目标名不存在；只处理主库本身。
8. **旧边车分别改名隔离**：对存在的 `novelcraft.db-wal`、`novelcraft.db-shm`、`novelcraft.db-journal` 逐个改名，分别使用 `novelcraft.db-wal.stale-<operation_id>`、`novelcraft.db-shm.stale-<operation_id>`、`novelcraft.db-journal.stale-<operation_id>`；第 3 步试开产生的边车也纳入本清单；禁止通配符、递归或批量处理。每个 stale 目标名必须**预先确认不存在**；已存在时不得覆盖、不得删除、不得自动加序号，立即停止并保留现场。
9. **恢复文件落位**：把**第 5 步的 staging 副本**（不是真实快照文件，也不得重新读取真实快照）复制到正式库同卷、同目录的临时名 `novelcraft.db.restore-<operation_id>.tmp`；复制后比较字节大小与 `manifest.snapshot_size`；**禁止跨卷 copy+rename**；**禁止把真实快照文件直接复制进 `backend/` 目录**。
10. **最终改名**：确认临时文件与目标同卷同目录后，用单次 rename 把临时名改为 `novelcraft.db`；rename 失败立即停止，保留旧文件、stale 边车与临时文件。
11. **恢复服务并检查**：按第 8、17 节启动后端，检查 `/health`、`/health/live`、`/health/ready`、`/api/health`；记录 PID、监听地址、启动时间、`source_hash`、`schema_version`；任一检查失败立即停止，不自动进行第二次恢复。若新库已被启动或迁移写过，按第 16.8 节处置；只有在**独立回滚授权**后，才可把新库及其当前边车整体隔离为 `.failed-<operation_id>`（见第 16.11 节第 9 条）。
12. **任一步失败**：停止，保留现场（旧文件、stale 边车、临时文件、pre-restore 快照），记录失败步骤与现象；不得删除、不得回滚到一半、不得把失败改成“已完成”；第 7 步之后的失败按第 16.8 节处置。

### 16.2 同卷规则与原子性

1. 临时恢复文件必须与 `backend/novelcraft.db` 位于**同一卷**；
2. 只允许“同卷、同目录的 rename”作为最终原子替换；
3. **不允许**使用跨卷 copy+rename 作为原子替换；
4. 跨卷目标只能作为人工重新设计后的非原子流程，当前正式恢复流程**禁止**使用；
5. pre-restore 快照可以与正式库同盘，但同盘**不能防御磁盘损坏**；异地或异盘备份仍未实现；
6. 同卷检查不能只比较盘符或路径前缀，必须使用文件系统卷标识或等价的可靠检查（如 Windows 卷序列号 / `GetVolumePathName` / 文件系统设备号）；正式库、临时文件与最终目标必须落在同一卷、同一目录。

### 16.3 旧文件与现场保护

1. 旧正式数据库只能改名隔离，不得直接删除；
2. 旧 `-wal`、`-shm`、`-journal` 只能改名隔离，不得直接删除；
3. 成功恢复并完成第 17 节验收之前，不得删除旧文件、边车、隔离文件、pre-restore 快照或失败现场；
4. 禁止通配符、递归和批量清理；
5. 删除必须另批授权，逐个精确路径确认。

### 16.4 试开与边车顺序

1. 独占试开结束后，必须重新检查并记录所有相关边车；
2. 任何试开产生的边车都必须纳入第 16.1 节第 8 步的隔离清单；
3. 不在真实快照目录中打开数据库；
4. 隔离副本统一使用 `mode=ro`，不使用 `immutable=1`。

### 16.5 正式目标 reparse point 守卫

1. 正式目标必须解析到当前项目的 `backend/novelcraft.db`；
2. 正式数据库路径及其父路径不得为 symlink、junction 或其他 reparse point；检查必须逐级覆盖到仓库根，**包含 `backend` 目录本身**；
3. 目标已存在时，必须检查其现有路径本身不是链接；
4. 临时文件名必须**不存在**，且其父目录不得解析为链接；
5. 最终 rename 前后必须再次确认父目录未被替换（至少核对卷标识与目录是否仍是同一对象）；
6. 检查必须在任何替换、打开或目录操作之前完成；守卫拒绝时不得创建任何文件或目录。

### 16.6 恢复前取证清单（只记录，不读业务数据）

- 正式库及边车：路径、大小、mtime，必要时 SHA-256（只计算文件字节，不解析内容）；
- 服务：后端与前端 PID、监听地址、检查时间；
- 操作：操作者、授权信息、源快照目录名、源 HEAD、pre-restore 快照目录名；
- 禁止：读取表、行、字段，导出业务数据，或输出凭据。

### 16.7 硬守卫清单（全部满足才允许执行）

1. 后端已停止，8000 无 LISTENING；
2. `.venv` shim 父进程、uvicorn 子进程及其他指向本仓库的 Python 进程均已退出；
3. 没有 pytest、快照脚本、演练脚本或恢复脚本正在访问正式库；
4. 已完成第 16.1 节第 3 步的独占试开，并重新记录边车；
5. 已创建并核对 pre-restore 快照，目录名与 `manifest.label` 均符合第 6 节；
6. 源快照目录名符合 `novelcraft-db-YYYYMMDD-HHmmss-<7位小写十六进制>`；
7. `manifest.json` 存在且可解析；
8. `snapshot_file` 必须是 `novelcraft.db`；
9. `snapshot_size` 必须等于源文件实际大小；
10. `integrity_check` 与 `quick_check` 必须在**隔离副本**上为 `ok`；
11. 正式库路径必须解析到当前项目的 `backend\novelcraft.db`，且目标与父路径均非 reparse point；
12. 旧 `-wal`/`-shm`/`-journal` 已按第 16.1 节第 8 步改名隔离，未直接删除；
13. 临时恢复文件与正式库同卷、同目录；
14. 替换动作失败时，不得删除 pre-restore 快照；
15. 恢复成功前，不得删除任何旧文件、旧快照或隔离边车；
16. 整个恢复动作单独授权，并记录操作时间、执行人、源快照目录。

### 16.8 第 7 步之后失败：现场状态与处置

第 7 步（旧主库改名）之后，任一步失败，正式数据库目录视为**不可启动状态**：

1. 不得启动后端（包括 `autostart_backend.bat`），不得让任何进程打开该库；
2. 不得人工把旧文件改回原名，不得删除、移动、覆盖或回滚任何文件；
3. 必须保留全部现场：旧主库、全部 stale 边车、临时恢复文件、pre-restore 快照，以及失败发生在哪一步；
4. 现场记录必须写明**实际路径**，例如 `backend/novelcraft.db.stale-<operation_id>`、各 `-wal/-shm/-journal` 的 stale 名、`novelcraft.db.restore-<operation_id>.tmp`、pre-restore 快照目录的完整路径，并记录每项的大小、mtime、记录时间与操作者；
5. 只有**重新授权**后才允许诊断或回滚；回滚必须是单独设计、单独批准的流程，不得在恢复会话里顺手做（回滚状态、operation id 与人工裁定边界见第 16.11 节）；
6. 在不改变数据库内容的前提下继续隔离剩余边车：默认禁止；只有新授权明确允许“仅做改名隔离”时才可执行，并逐项记录；
7. 不得用“先启动服务看看”作为诊断手段。
8. **失败新库的处置（与第 16.11 节第 9 条一致）**：若新库已就位、已被启动或迁移写过，必须在**独立回滚授权**后，把新库及其**当前**`-wal`/`-shm`/`-journal` 作为一个整体隔离为 `novelcraft.db.failed-<operation_id>` 组；隔离前先确认无句柄并重新扫描边车；目标名与 failed 名都必须预先确认不存在，冲突即停止，不覆盖、不删除、不自动加序号；该隔离动作不属于自动回滚，也不代表回滚已完成。

### 16.9 已验证 staging 副本：保留与交接

现有 `drill-db-restore.py` 在成功后**删除整个演练目录**，且没有“保留成功现场”或“输出到指定 staging 目录”的参数。
因此它**不能直接承担**第 16.1 节第 5 → 9 步的数据流；正式流程必须使用下面这条**专用手工 staging 流程**。

1. **创建 staging 目录**：位于当前用户 TEMP 根下，命名 `novelcraft-restore-staging-<时间戳>-<随机后缀>`；不得位于仓库内、真实快照根内或任何已有快照目录内；创建前确认不存在；本地路径检查、非 UNC/设备路径、父路径逐级非 symlink/junction/reparse。
2. **复制前记录真实快照源指纹**：记录真实快照 `novelcraft.db` 的**大小、mtime、SHA-256**；同时确认大小等于 `manifest.snapshot_size`。只读取文件字节，**不打开真实快照数据库、不执行 SQL**。
3. **只复制一个文件**：从真实快照复制 `novelcraft.db` 到 `<staging>\novelcraft.db`；不复制 `manifest.json`、不复制 `-wal`/`-shm`/`-journal`。
4. **复制后再次记录真实快照源指纹**：再次记录源文件的大小、mtime、SHA-256；**size 与 SHA-256 必须与第 2 步一致**。任一不一致，即判定“复制期间源发生变化”。mtime 仅作辅助记录：mtime 变化但 size 与 SHA-256 均未变时，记录前后 mtime 后继续。
5. **记录 staging 指纹并与源比对**：记录 staging 副本的大小、mtime、SHA-256；大小必须等于 `manifest.snapshot_size`，且 **staging 的 SHA-256 必须等于第 4 步复制后的源 SHA-256**；mtime 记录但不作为失败条件。这才能证明 staging 是某个稳定源文件的完整副本，而不只是“自身可校验”。
6. **任一比对失败立即停止**：不得进入第 7 步及之后的正式替换步骤（只读取证、记录失败项与人工报告仍应继续）；不得重试同一源，不得静默改用另一份快照；保留 staging 现场，按第 16.10 节记录源与 staging 的完整路径、大小、mtime、SHA-256 与判定失败的具体项。
7. **只读校验**：`file:<staging 副本>?mode=ro`，只跑 `PRAGMA integrity_check` 与 `PRAGMA quick_check`；不使用 `immutable=1`；不执行 `SELECT`；校验只在 staging 目录内进行，不在真实快照目录内打开数据库。
8. **保留到交接完成**：校验通过后，staging 副本必须保留到第 9 步（落位临时文件）完成；期间不得删除、移动或改写。
9. **第 9 步只认 staging**：落位只能从该 staging 副本复制；**不得重新读取真实快照**，不得把真实快照直接复制进 `backend/`；落位后临时文件的大小必须等于 staging 大小。
10. **成功后的处理**：正式恢复通过第 17 节验收前，staging 目录与文件必须保留；验收通过后仍不自动删除，删除必须单独授权、精确路径、逐个确认。
11. **失败后的处理**：第 7 步之前（源指纹比对、staging 创建/复制/校验阶段）的失败，按第 16.10 节处理，**不适用第 16.8 节“正式数据库目录不可启动”的结论**；第 7 步及之后的失败才适用第 16.8 节。
12. **禁止用途**：staging 副本不得当作正式库直接启动服务，不得用于迁移演练之外的任何用途。
13. **工具化状态**：`scripts/stage-db-snapshot.py` 已按本节第 1–7 条实现 staging 副本创建与校验（源指纹前后比对、保留现场、输出 `STAGING_PATH`）；它没有正式替换、删除、移动、改名或清理能力，也没有清理参数，成功与失败都保留 staging。`--dry-run` 只做路径与 manifest 校验并报告计划目录名与可用性（`STAGING_DIR`、`STAGING_TARGET_AVAILABLE`），**不创建目录、不复制、不读源字节、不算哈希、不打开数据库**。该工具**不能承担第 8–9 步**，也不得被扩展为恢复脚本；正式恢复仍按第 16.1–16.8、16.10 节手工执行。
14. **磁盘空间预留必须覆盖四处**：执行正式恢复前，目标卷与 TEMP 卷的剩余空间必须同时容纳：①staging 副本（1× 正式库）；②`backend/` 内同卷临时恢复文件（1×）；③pre-restore 快照（1×，`mode=ro` 在线备份）；④失败现场保留空间（旧库 stale 名 + 其 `-wal/-shm/-journal` + staging + pre-restore 可能同时存在，至少再按 1× 计）。因此：
    - **所有副本在同一卷**时，最低要求为 **≥4× 正式库大小**，不能用“两倍”过关；
    - **pre-restore 快照在另一卷**时，执行卷最低 ≥3×，另一卷 ≥1×；
    - 无论哪种，执行前都必须用卷剩余空间实测核对，不足即停止；不得边写边赌空间。

### 16.10 第 7 步之前失败：处置

本节适用于第 16.1 节第 0–6 步（尚未对正式库改名）阶段的失败，包括：源指纹前后比对不一致、staging 目录创建失败、复制失败、大小不符、staging 只读打开或 PRAGMA 检查失败。

1. **不启动、不影响现有服务**：正式库尚未被改名，8000 的状态取决于恢复前是否已按第 16.1 节第 1 步停止；无论处于哪种状态，都**不得**为“验证”而启动后端，也不得主动停止或重启前端。
2. **不删除任何现场**：staging 目录、部分复制的文件、源指纹记录、失败记录**一律保留**；不得删除、移动、改写或“清理干净”。
3. **必须记录**：源快照与 staging 的实际路径、大小、mtime、SHA-256；失败发生在哪一步、哪一项比对失败；记录时间与操作者。
4. **不得自动重试或换源**：不得重新读取同一真实快照重试，不得静默改用另一份快照；继续操作必须重新授权。
5. **staging 的后续处置需单独确认**：是保留、放弃还是销毁，都必须单独授权、精确路径、逐个确认；不得在本次会话里顺手清理。
6. **不适用第 16.8 节**：此时正式数据库目录仍处于恢复前状态，“不可启动、需重新授权、不得自行回滚”的 §16.8 结论不成立；正式替换尚未开始，不存在需要回滚的替换动作。
7. **正式库未受影响**：只要没有执行第 16.1 节第 7 步及之后动作，正式库及其边车应保持原状；如发现正式库被意外改动，立即停止并按第 16.8 节处理。

### 16.11 回滚流程：状态矩阵、operation id 与人工裁定边界

> 本节是**设计稿**，不是可执行脚本。回滚不属于恢复会话内的自动补救（第 16.8 节第 5 条），必须先取得单独授权；回滚完成后**不自动启动**服务。

1. **回滚是独立流程**：目标是把正式目录恢复到“可人工复核的旧库状态”，不是“自动修好”。回滚不得与正式恢复、清理、staging 处置混在同一批授权里。
2. **单一 operation id**：正式恢复开始前生成一次 `operation_id`（建议 `YYYYMMDD-HHmmss-<6位随机>`）。本次所有隔离与临时文件统一使用该 ID，不得为各文件分别生成时间戳：
   - 旧主库：`novelcraft.db.stale-<operation_id>`；
   - 旧边车：`novelcraft.db-wal.stale-<operation_id>`、`-shm`、`-journal` 同理；
   - 正式目录临时文件：`novelcraft.db.restore-<operation_id>.tmp`；
   - 失败的新库：`novelcraft.db.failed-<operation_id>`；
   - 失败新库的边车：`novelcraft.db-wal.failed-<operation_id>`、`novelcraft.db-shm.failed-<operation_id>`、`novelcraft.db-journal.failed-<operation_id>`。
3. **stale 组契约**：一组只允许四类成员——主库、`-wal`、`-shm`、`-journal`，且必须同一 `operation_id`。现场记录必须保存 `operation_id`、文件角色（主库/边车）、原始路径、实际路径、大小、mtime、SHA-256。
4. **多组、「缺组」与拼配禁止**：现场出现多个 `operation_id` 组、同组内同名成员重复、成员来源不明或现场记录缺失时，立即停止并交人工裁定；不得自动挑选“看起来最新”的一组，也不得跨组拼配主库与边车。**“缺组”按成员角色分别判定**：
   - **主库 stale 成员**（`novelcraft.db.stale-<operation_id>`）：必须存在且唯一。缺失、重复或来源不明 → 立即停止；
   - **`-wal` / `-shm` / `-journal` 边车**：属于可选成员，**缺失本身不等于组无效**，不得仅因某个边车不存在就判定失败；
   - 但若现场记录（第 3 条字段）表明该 `operation_id` 组曾存在某个边车，而现在该边车缺失 → 必须进入人工裁定，不得按“本来就没有”处理；
   - 无论何种情况，都不得跨 `operation_id` 拼配主库与边车。
5. **pre-rollback 快照按状态拆分**（S1–S6 正式目标可能不存在，不能用 `db-snapshot.py` 统一处理）：
   - **S7–S9（正式目标存在）**：创建 pre-rollback 快照（仓库外，`db-snapshot.py`），核对大小、`integrity_check`、`quick_check`；失败立即停止；
   - **S1–S6（正式目标缺失）**：不得要求从目标建快照。必须先记录现场指纹（各 stale、临时文件、failed 库、staging、既有快照），并保留全部现场；是否允许继续必须单独批准；
   - 禁止把 staging 或真实快照冒充 pre-rollback 快照；目标缺失状态下如需可回退点，另立“现场归档/现场快照”契约，不复用 `db-snapshot.py`。
6. **回滚源唯一**：只接受人工明确指定的一组 stale；禁止使用真实快照或 staging 作为回滚源；禁止用“从 pre-restore 快照恢复”冒充回滚。
7. **边车配套进入人工裁定**：回滚源组内 `-wal` 非空、`-wal` 缺失但主库曾以 WAL 模式使用、`-shm`/`-journal` 来源不明，任一出现即停止，交人工裁定；空 `-wal` 可记录后继续；`-shm` 缺失本身不单独阻断，但其与空/非空 WAL 的组合必须记录。工具不得自行判断 SQLite 配套关系。
8. **恢复顺序与中间失败**：先隔离失败的新库及其边车（若存在），再 rename 主库，再逐个 rename 边车。每次 rename 前后更新现场记录；**主库恢复成功不代表回滚成功**；任一边车恢复失败，立即进入不可启动现场，停止：不得继续尝试其他边车，不得启动服务，不得把已恢复的主库再次改走；后续处理重新授权。
9. **S8/S9 新库整体隔离**：新库与其当前 `-wal`/`-shm`/`-journal` 必须作为同一失败组整体隔离，成员精确命名为 `novelcraft.db.failed-<operation_id>`、`novelcraft.db-wal.failed-<operation_id>`、`novelcraft.db-shm.failed-<operation_id>`、`novelcraft.db-journal.failed-<operation_id>`；**只有实际存在的边车才进入 failed 组，缺失边车不创建占位文件**；每个 failed 目标名必须**预先确认不存在**，任何冲突立即停止，不覆盖、不删除、不自动加序号；隔离前先停止并确认无句柄，再重新扫描边车；**failed 组不得与 stale 组混用，也不得作为回滚源**。
10. **不删除、不自动二次回滚**：全程只允许受守卫的同卷 rename；不删除任何文件；失败保留全部现场（stale、临时文件、failed 库、pre-rollback 或现场指纹记录）。
11. **回滚后不自动启动**：启动与“迁移可能写库”属于独立授权步骤（第 17 节）；回滚完成后先离线核对主库与边车指纹，再决定是否申请启动。
12. **回滚前硬守卫**：后端已停止且 8000 无 LISTENING；无其他进程持有正式库；目标与每级父路径非 symlink/junction/reparse；临时文件与目标同卷同目录；目标名不存在或已明确标记为失败现场；已锁定唯一 `operation_id` 组；现场已记录；已按第 5 条完成 pre-rollback 或现场指纹记录。

### 16.12 恢复操作记录契约

本节定义正式恢复全过程唯一的一份操作记录。回滚依赖它判断“边车是否曾存在”、文件角色与指纹，因此它必须在恢复开始前创建，而不是事后补写。

1. **固定位置**：`%USERPROFILE%\NovelCraft-backups\operations\<operation_id>.json`，完整性文件为同目录 `<operation_id>.json.sha256`。不得位于仓库内、快照根内或 staging 目录内；不得提交 Git。
2. **创建时机与写入者**：第 16.1 节第 0 步取得授权后立即创建；写入者是执行正式恢复的人（或未来经单独授权的恢复工具）。**任何回滚/盘点工具不得代写、补写或修正该记录。**
3. **更新方式**：每完成或失败一步，整份记录以“临时文件 + 原子替换”更新一次；不保留多份版本，不在仓库内留副本。任一步开始前写入 `current_step` 与 `step_status=in_progress`，结束后改为 `succeeded|failed`。
4. **最小 schema（`record_schema_version: "1.0"`）**：
   - 顶层：`operation_id`、`created_at`、`updated_at`、`started_at`、`finished_at`、`operator`、`authorization_ref`（脱敏标识，不含凭据）、`current_step`（0–12）、`step_status`、`state`（S0–S9）、`head_at_start`、`source_snapshot_dir`、`pre_restore_snapshot_dir`、`tool_version`；
   - `service`：`backend_pid_before`、`backend_pid_after`、`frontend_pid`、`listen_8000_before`、`recorded_at`；
   - `files[]`：`role`（`main`/`wal`/`shm`/`journal`/`failed_new`/`failed_wal`/`failed_shm`/`failed_journal`/`temp_restore`）、`original_path`、`actual_path`、`exists`、`size`、`mtime`、`sha256`、`volume_id`、`reparse`、`recorded_at`、`record_source`；
   - `groups`：`stale[]`、`failed[]`、`temp[]`（均为 `files[]` 的引用或内联副本）；
   - `renames[]`：`seq`、`role`、`from`、`to`、`before{size,mtime,sha256}`、`after{size,mtime,sha256}`、`at`；
   - `failures[]`：`step`、`category`、`at`、`note`（只写类别与位置，不写业务内容）。
5. **完整性**：`.json.sha256` 保存对“规范化 JSON”（键排序、UTF-8 无 BOM、无尾随空白）的 SHA-256。读取时必须校验；记录缺失、损坏、哈希不符、`operation_id` 不匹配或必需字段缺失时，**只能返回人工裁定**，不得猜测、不得用“最新文件”替代、不得从文件名反推补写。
6. **内容边界**：只允许路径、大小、mtime、指纹、状态与脱敏标识；禁止业务数据、表名、字段名、行内容、`.env`、连接串、密码、token、Cookie。
7. **归属与保留**：记录属于恢复现场，未获单独授权不得删除、移动、覆盖或“清理干净”；它不是数据库快照，不能用于恢复；删除必须精确路径、逐个确认。
8. **读取守卫**：盘点器读取本记录及其 `.json.sha256` 必须遵守第 16.14 节；任何越界、reparse、配对不符、哈希失败或字段缺失，一律进入人工裁定。

### 16.13 S1–S6 现场归档契约（只读元数据）

S1–S6 的目标是“正式目标缺失、无法用 `db-snapshot.py` 建快照”。此时只能用**元数据清单**固定现场，不复制数据库文件。

1. **固定位置**：`%USERPROFILE%\NovelCraft-backups\operations\<operation_id>-scene.json`，完整性文件为 `<operation_id>-scene.json.sha256`；仓库外，不提交 Git。
2. **生成时机**：在第 16.10 节失败处置或回滚前盘点阶段，取得“现场归档”单独授权后生成一次；不自动、不重试、不覆盖既有 scene。
3. **最小内容（`scene_schema_version: "1.0"`）**：`operation_id`、`created_at`、`created_by`、`state`、`failed_step`、`failed_at`、`record_path`、`record_sha256`、`tool_version`；正式目录清单（`novelcraft.db`、三个边车、stale 成员、`restore-*.tmp`、`failed-*` 的 `name`/`exists`/`size`/`mtime`/`sha256`/`volume_id`/`reparse`）；目录对象信息（目录 mtime 与可获得的 `st_dev`/file index 近似值，须标注“近似”）；服务与监听记录（后端/前端 PID、8000 是否 LISTENING、记录时间）。
4. **只读边界**：只做 `stat` 与按字节计算 SHA-256；**不打开数据库、不执行 SQL/PRAGMA、不复制任何数据库文件**；不建仓库内文件。写 scene 文件本身是该步骤唯一副作用，必须原子写（临时文件 + 替换），失败不得留下半文件。
5. **不是快照**：scene 不能用于启动、不能当作恢复源、不是回滚点；数据库副本如需另存，必须另立契约并单独授权。
6. **v1 回滚工具约束**：只读盘点器**可以读取** record 与 scene，但**不得写入、修正、补全**任何一个；记录缺失时只能输出“仅指纹 + 人工裁定”，不得自行生成替代记录；不得复制数据库文件、不得打开数据库。
7. **读取守卫**：scene 及其 `.json.sha256` 的读取必须遵守第 16.14 节；任何越界、reparse、配对不符、哈希失败或字段缺失，一律进入人工裁定。

### 16.14 记录文件读取守卫（适用于 16.12 与 16.13）

回滚盘点器读取操作记录或现场归档时，必须满足以下全部条件；任一不满足即进入人工裁定，不得降级读取、不得改用其他文件。

1. **路径类型先行**：在 `Path()` 解析之前拒绝 UNC（`\\server\share`、`//server/share`）、设备路径（`\\.\` 前缀、`\\?\` 且非 `\\?\C:` 盘符形式）以及含 `..` 的输入。
2. **固定根约束**：唯一允许的记录根是 `%USERPROFILE%\NovelCraft-backups\operations`（由 `Path.home()` 推导，不接受任何参数覆盖）。校验方式是**完整路径相等**，不是“末尾目录名相同”：对输入根与期望根分别做逐级 reparse 检查、自身 reparse 检查与 `lstat` 目录校验，再用 `os.path.normcase(os.path.abspath(...))` 规范化比较；Windows 按大小写不敏感处理。`D:\other\NovelCraft-backups\operations`、仓库内同名目录、其他用户 home 下的同名路径一律拒绝（退出码 3）；期望根不存在或无法取得身份时转人工裁定（退出码 6）。不使用 `resolve()` 作为唯一依据。
3. **逐级 reparse 检查**：根目录、每一级父路径、目标 `.json`、`.json.sha256` 以及 scene 与其 sidecar 都不得是 symlink、junction 或其他 reparse point；检查必须**先于**打开或读取。
4. **精确配对**：`<operation_id>.json` 只能与 `<operation_id>.json.sha256` 配对；`<operation_id>-scene.json` 只能与 `<operation_id>-scene.json.sha256` 配对；文件名中的 id 必须等于所选 `operation_id`；禁止通配符扫描后“挑一个”，禁止跨 id 配对。
5. **校验顺序**：路径守卫 → 存在性 → 常规文件（非目录）→ 大小上限（防御异常大文件）→ 只读读取字节 → SHA-256 与 sidecar 比对 → JSON 解析 → schema 与必需字段校验。
6. **不跟随链接读取**：先 `lstat` 判定非 reparse，再以只读方式打开；打开后尽可能复核文件标识/卷与 `lstat` 一致；无法复核时不得继续，转人工裁定。
7. **失败统一处理**：越界、reparse、配对不符、哈希不符、解析失败、字段缺失、`operation_id` 不匹配，全部返回人工裁定（退出码 6）；**不创建替代记录、不补写、不修正、不从文件名推断状态、不回退到其他 id、不把记录内容回显到仓库或日志正文。**
8. **只读**：读取过程不得写、删、移、改任何记录或 sidecar；不得把记录内容复制进仓库。

---

## 17. 恢复后的实际行为与检查

1. 启动时应用可能执行迁移或初始化逻辑，**快照 schema 落后于当前代码时尤其如此**；
2. 迁移会写数据库，属于恢复动作的一部分，必须纳入同一份正式恢复授权；
3. 前端可以继续运行，但恢复窗口内 `/api` 请求失败属于预期；
4. 启动后依次检查：`/health`、`/health/live`、`/health/ready`、`/api/health`；
   四个端点的实现均已存在于 `backend/app/routers/health.py`（`/health/live` 为纯存活探针，不访问数据库；`/health/ready` 执行 `SELECT 1`；裸 `/health` 是版本识别接口）；
5. 记录 PID、监听地址、启动时间、`source_hash`、`schema_version`；
   `schema_version` 来自裸 `/health` 内的只读查询 `SELECT COALESCE(MAX(version), 0) FROM schema_migrations`（失败时回退 `PRAGMA user_version`）；
   因此该接口**会以只读方式访问数据库**，不是零副作用端点；
6. 不自动读取业务数据；需要业务验证时另行授权；
7. 健康检查失败立即停止，不自动进行第二次恢复；
8. 保留 pre-restore 快照与现场，等待人工决定。

---

## 18. 当前工具边界汇总

| 工具 | 能做 | 不能做 |
|---|---|---|
| `scripts/db-snapshot.py` | 在线创建 SQLite 快照、完整性检查、写 manifest | 恢复、删除、保留策略、上传 |
| `scripts/report-db-snapshots.py` | 只读报告快照清单与分类建议 | 删除、移动、打开数据库、重跑完整性检查 |
| `scripts/stage-db-snapshot.py` | 从指定快照创建经过指纹和 SQLite 完整性验证的 staging 副本，并保留供交接 | 替换、删除、清理、恢复、迁移、服务操作或业务数据读取 |
| `scripts/drill-db-restore.py` | 复制单份快照到 TEMP 并做只读完整性检查 | 替换正式库、删除快照、接触正式库 |
| `scripts/rollback-db.py` | 只读盘点 operation record、scene、stale/failed/temp 现场，生成状态与回滚守卫报告 | rename、删除、复制、打开数据库、SQL、迁移、服务操作、自动回滚 |
| Git bundle | 备份代码历史 | 备份数据库 |
| 本文件 | 描述手工恢复流程与守卫 | 提供恢复脚本 |

- 数据库快照是**完整业务数据**，可能含账号与正文，不能外发、上传或提交 Git；
- 正式恢复没有脚本可用，仍是手工、高风险、单独授权的操作；
- `scripts/rollback-db.py` 的退出码：`0` 计划可生成（仅当全部守卫可验证且通过）、`2` 参数错误、`3` 路径/链接/卷/目录对象等硬守卫失败、`4` I/O 失败、`5` record 与现场不一致、`6` 需人工裁定（含守卫无法验证、inventory 被拒、真实服务状态不可验证）、`7` 状态不满足回滚前置条件（含 pre-rollback 记录不完整）、`8` 工具内部错误；任何非 `0` 都不得据此执行回滚；
- `scripts/rollback-db.py` 对**正式数据库及其 live 边车只读取文件系统元数据**（`exists`、`is_file`、`size`、`mtime_ns`、`volume_id`、`reparse`），不计算内容哈希、不打开数据库、不执行 SQL/PRAGMA；`stale`/`failed`/`temp` 文件是否计算 SHA-256 按大小上限与操作记录规则处理；操作记录若要求 live 的 SHA-256，工具报告“不适用/人工裁定”，不会为满足记录而读取正式库；
- `scripts/rollback-db.py` 在读取/扫描前后分别对 `backend` 与 `operations` 目录执行不跟随链接的 `lstat`，记录并比较 `st_dev`、`st_ino`、`mtime_ns` 与 reparse 状态；任一项变化（含被替换为另一个合法的非 reparse 目录）即返回人工裁定（exit 6）或路径守卫错误，不继续生成计划；该工具不把单独 `mtime` 当作身份依据，目录身份无法取得时输出 unknown 并阻断；inventory 模式与指定 `operation_id` 模式都执行同一检查；
- **竞态边界**：v1 只读盘点器的前后目录身份检查只覆盖“检查窗口内”的替换，**不提供全程原子一致性**；即使最终检查通过，检查完成后目录仍可能被外部替换，这部分属于平台竞态残余风险。因此盘点结果只能用于人工评估，**任何正式 rename/替换仍必须在执行时重新执行独立的硬守卫**，不得复用盘点结论直接执行，也不得把盘点通过当作执行授权。
- 允许读取内容的文件（record/scene/sidecar/pre-rollback、`stale`/`failed`/`temp`）统一走受保护读取：`lstat` 后以 `os.open`（可用平台附带 `O_NOFOLLOW`）打开，读取前用句柄 `fstat` 比对 `st_dev`/`st_ino`/`size`/`mtime_ns`，读取后再比对一次并重新 `lstat`；不一致即中止并进入人工裁定（exit 6），不重试、不换路径。**平台限制**：Windows 没有 `O_NOFOLLOW`，因此不能声称“路径检查与打开原子一致”；该平台依靠句柄身份比对，若身份不可获得则保守返回人工裁定，不把预检/后检描述为原子保证。

---

## 19. 隔离恢复演练工具

新增 `scripts/drill-db-restore.py`，把**一个**指定快照的 `novelcraft.db` 复制到 TEMP 下的新建目录，并在副本上做只读完整性检查。

```powershell
<repo>\backend\.venv\Scripts\python.exe scripts\drill-db-restore.py --snapshot-dir "%USERPROFILE%\NovelCraft-backups\database\novelcraft-db-20261101-135316-4d45595" --dry-run
<repo>\backend\.venv\Scripts\python.exe scripts\drill-db-restore.py --snapshot-dir "<同上>"
<repo>\backend\.venv\Scripts\python.exe scripts\drill-db-restore.py --snapshot-dir "<同上>" --json
```

1. 工具只把快照复制到当前用户 TEMP 下的隔离目录（`nc-restore-drill-<时间戳>-<随机后缀>`），副本固定为 `<演练目录>\restored\novelcraft.db`；
2. 不替换正式数据库，不接受 `--target-database`、`--restore`、`--replace`、`--delete`、`--clean`、`--source-database` 等参数；
3. 不停止、不启动服务，不接入应用；
4. 不打开源快照中的数据库文件，只读取源文件大小与 mtime；`manifest.json` 只读允许字段，不读 `backup_sha256`；源路径及其任一父路径为 symlink、junction 或 reparse point 时直接拒绝（exit 3），`--temp-root` 同理；
5. 副本用 `file:<path>?mode=ro` 打开，**不使用 `immutable=1`**（WAL 快照下会打不开）；只执行 `PRAGMA integrity_check` 与 `PRAGMA quick_check`，不执行任何 `SELECT`；
6. 成功时删除本批演练目录（先校验目录在 TEMP 下、名字符合约定、内容只含本批文件）；
7. 失败时默认保留现场并报告演练目录名，便于人工检查；当前 CLI **没有** `--keep-on-failure`，也没有关闭保留或自动清理的参数；
8. `--dry-run` 只校验快照目录与 manifest，不创建目录、不复制、不打开数据库；
9. 退出码：`0` 成功并清理、`2` 参数错误、`3` 源快照或路径校验失败、`4` 建目录或复制失败、`5` 副本大小不一致、`6` 只读打开或完整性检查失败、`7` 清理失败保留现场；
10. 输出只含状态、非敏感错误类别、源目录名、演练目录名、副本文件名、`snapshot_size`、副本大小、两个 PRAGMA 结果、边车情况、是否清理、HEAD（若能安全读取）与工具版本；不含完整绝对路径、用户名、连接串、凭据或数据库内容；
11. 该工具**没有正式恢复能力**，也不能证明业务数据正确或应用能启动——它只证明副本文件结构可读且通过两个 PRAGMA；
12. 真实快照不得直接用 SQLite 打开验证，应通过本工具复制到 TEMP 后检查；
13. 不要把本工具当作恢复工具，也不要把它的通过结果当作发布许可；
14. 正式恢复仍按第 5–8 节手工执行，并遵守第 16 节的守卫；
15. 本工具成功后会**删除整个演练目录**，没有“保留成功现场”或“输出到指定 staging 目录”的参数；正式恢复的 staging 必须按第 16.9 节手工执行，**不得用本工具替代**。

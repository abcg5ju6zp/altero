# 快照一致的文献库导出 - Independent Review

- [x] CP-1: 数据库内容单一快照绑定
  - **Type**: `rule`
  - **Covers**: AC-1
  - **Evidence**: R1 pass — PostgreSQL 走 REPEATABLE READ（真实 PG 18 容器实测 transaction_isolation=repeatable read）；SQLite 导出会话显式 BEGIN 建立单一 WAL 快照；_collect_snapshot 覆盖全部对象/关系，_build_documents 的关系懒加载在快照事务内存活。

- [x] CP-2: 并发写入行为正确
  - **Type**: `rule`
  - **Covers**: AC-2
  - **Evidence**: R1 pass — test_export_reads_the_old_snapshot_while_a_write_commits（归档等于边界版本）；test_a_serialization_failure_aborts_the_export（40001 → ExportConflictError，无文件/残留）；PG 并发套件 6 项通过。

- [x] CP-3: 附件边界校验
  - **Type**: `rule`
  - **Covers**: AC-3
  - **Evidence**: R1 pass — 消失三阶段（read 前/中、最终校验）→ AttachmentMissingError；替换（size/mtime_ns 不符）→ AttachmentChangedError；故障注入两测试通过，中止后清理、destination 不存在。

- [x] CP-4: 校验后原子发布
  - **Type**: `rule`
  - **Covers**: AC-4
  - **Evidence**: R1 pass — _validate_archive 严格 CRC/成员集合/8 项 counts；同目录 os.replace + fsync；manifest 最后写入；成功路径无工作区残留。

- [x] CP-5: 临时写入失败处理
  - **Type**: `rule`
  - **Covers**: AC-5
  - **Evidence**: R1 pass — FailingZip ENOSPC → ExportWriteError，destination 不存在、无工作区；中止类异常正确穿透。

- [x] CP-6: 发布失败处理
  - **Type**: `rule`
  - **Covers**: AC-6
  - **Evidence**: R1 pass — os.replace EIO 注入 → ExportPublishError，工作区清理、destination 不存在。

- [x] CP-7: 重启回收
  - **Type**: `rule`
  - **Covers**: AC-7
  - **Evidence**: R1 pass — recover_workspaces 旧删/新留/sidecar 缺失回退目录 mtime/单项失败隔离；lifespan 异常捕获不阻断启动。

- [x] CP-8: 已确认归档保护
  - **Type**: `rule`
  - **Covers**: AC-8
  - **Evidence**: R1 pass — 回收仅处理 `.altero-export-*` 目录；已发布 .zip 字节不变；符号链接报错跳过。

- [x] CP-9: 下载端隔离
  - **Type**: `rule`
  - **Covers**: AC-9
  - **Evidence**: R1 pass — 全新会话导出；仅成功后 FileResponse；409 JSON + 导出目录为空、工作区 URL 404、下载后发布文件删除。

- [x] CP-10: 空库往返
  - **Type**: `rule`
  - **Covers**: AC-10
  - **Evidence**: R1 pass — counts 全 0、files==[]、成员恰为 manifest+七文档；导入后 is_empty。

- [x] CP-11: digest 去重
  - **Type**: `rule`
  - **Covers**: AC-11
  - **Evidence**: R1 pass — 两附件同 md5：files 成员一、manifest.files 一条、counts.files=1、sha256/size 正确。

- [x] CP-12: 格式版本与旧归档兼容
  - **Type**: `rule`
  - **Covers**: AC-12
  - **Evidence**: R1 pass — 去除 files/exportedAt 旧归档导入成功（版本 37）；FORMAT_VERSION 保持 1（PG/SQLite 实测）；清单不一致→"do not match"、files 非 list→"malformed"，均写行前拒绝；未来格式/非 ZIP 拒绝保持。

- [x] CP-13: 权限检查不变
  - **Type**: `rule`
  - **Covers**: AC-13
  - **Evidence**: R1 pass — 401/403/404/管理员 200/成员 403/API key 401 断言语义未改，全部通过。

- [x] CP-14: CLI 回收命令
  - **Type**: `rule`
  - **Covers**: AC-14
  - **Evidence**: R1 pass — `library recover-exports [path ...] [--grace]` 注册并通过两测试：指定路径与默认 export_path。

- [x] CP-15: 失败可诊断性
  - **Type**: `rubric`
  - **Covers**: AC-15
  - **Scale**: 1-5
  - Anchors: 1 = 错误混同、无定位信息；3 = 类型可区分但缺上下文；5 = 独立类型 + message/日志含库、digest、阶段
  - **Pass Threshold**: >= 4
  - **Evidence**: R1 — 得分 5/5。五类失败独立异常；message/日志含库 type/owner、digest/路径、阶段；HTTP 409/500 映射经测试确认。

- [x] CP-16: 资源清理完整性
  - **Type**: `rubric`
  - **Covers**: AC-16
  - **Scale**: 1-5
  - Anchors: 1 = 失败后残留；3 = 主路径清理但个别残留；5 = 全部路径无残留且重启回收旧残留
  - **Pass Threshold**: >= 4
  - **Evidence**: R1 — 得分 5/5。成功仅留最终归档；五失败路径 glob 断言为空；重启回收覆盖旧残留；测试后仓库根无 exports/storage/altero.sqlite 残留。

## Review History

### Review R1
- **Result**: `pass`
- **Reviewer**: 全新独立上下文（general_purpose_task），只读审查，未修改仓库文件。
- **Evidence**:
  - CP-1..CP-14（rules）全部 pass；CP-15/CP-16（rubrics）5/5；16 个 AC 均有独立证据。
  - PostgreSQL 路径在真实 PG 18 容器独立验证（REPEATABLE READ、快照内容、export/import 往返）。
  - 实测命令：tests/test_transfer.py 40 passed；tests/test_web_transfer.py + test_architecture.py 249 passed；PG 下 test_concurrency.py 6 passed；test_app.py 2 passed；仓库根无残留；GetDiagnostics 0 问题。
  - 全量套件证据（Implement 阶段）：2760 passed, 18 skipped, EXIT:0（482.73s）。
- **Findings**: 无 actionable；6 项 advisory（组装校验失败 400 归类、sidecar 写入错误未包装、_build_documents 懒加载序列化包装、PermissionError 归类、超 grace 长导出回收、sha256/size 导入不校验），均不影响安全性/清理/原子性，部分为 spec 已接受的取舍；不阻塞发布。

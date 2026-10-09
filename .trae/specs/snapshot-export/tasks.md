# 快照一致的文献库导出 - 实现计划

## Task 1: 导出失败异常体系与导出目录配置
- **Status**: `completed`
- **Priority**: high
- **Depends On**: None
- **Description**:
  - 在 errors.py 新增导出专用异常：`ExportAbortedError`（中止基类）及其子类 `ExportConflictError`（快照/序列化失效）、`AttachmentMissingError`（边界内附件消失）、`AttachmentChangedError`（边界内附件被替换）；另新增 `ExportWriteError`（临时归档写入失败）、`ExportPublishError`（最终发布失败）。各异常携带结构化上下文属性（库 type/owner、digest 或路径），message 人类可读。
  - api/errors.py 的 STATUS_CODES 增加 `ExportAbortedError → 409`（写入/发布失败类回退 500）。
  - settings.py 新增 `export_path: Path = Path("./exports")`（服务端导出工作区与已发布下载文件的根目录）。
  - tests/conftest.py 的 settings fixture 将 export_path 指向 tmp_path，防止测试污染工作区。
  - config.example.py 同步展示新配置项。
- **Acceptance Criteria Addressed**: AC-5, AC-6, AC-15
- **Test Requirements**:
  - `rule` TR-1.1: 六类异常可实例化且保留上下文属性；`status_for(ExportConflictError(...)) == 409`，`status_for(ExportWriteError(...)) == 500`，`status_for(AttachmentMissingError(...)) == 409`。证据：新增/现有单元测试。
  - `rule` TR-1.2: Settings 默认 export_path 为 ./exports；conftest fixture 产出的路径在 tmp_path 下。证据：测试断言 + 全测试套件不再在工作区生成 exports/。
- **Notes**: AttachmentMissingError/AttachmentChangedError 同属 ExportAbortedError 以共享 409 语义，但类型彼此独立用于诊断。
- **Completion Evidence**:
  - TR-1.1 pass：TestExportFailureContract 4 项测试通过（uv run pytest，2026-10-09），覆盖 409/500 映射与上下文属性。
  - TR-1.2 pass：默认值断言通过；conftest settings fixture 已含 export_path=tmp_path/"exports"，后续全量回归验证无污染。

## Task 2: REPEATABLE READ 快照内核（数据库读取重构）
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 1
- **Description**:
  - 重构 transfer.py 的 `export_library`：在任何查询之前为会话建立 REPEATABLE READ 隔离级别（SQLAlchemy execution_options；PostgreSQL 生效，aiosqlite 接受且 WAL 下读快照一致）；先读取库行记录边界版本，再在同一事务中完成全部对象与关系查询（items/collections/searches/tags/settings/fulltext/deletions 及 CollectionItem/ItemTag/各 relations）。
  - 七份 JSON 文档的字段与排序保持现状。
  - 事务内完成读取后做边界版本复核（同一快照复读库版本必须一致）；包装 DBAPI 序列化/快照失效错误为 ExportConflictError。
  - 导出保持只读，不提交写入。
- **Acceptance Criteria Addressed**: AC-1, AC-2
- **Test Requirements**:
  - `rule` TR-2.1: 导出进行中，用另一会话对目标库执行一次写入并提交，所得归档的库版本、对象集合与内容全部等于写入前边界版本。证据：并发测试（SQLite WAL）。
  - `rule` TR-2.2: 注入序列化失效（查询路径抛 PostgreSQL SQLSTATE 40001 类 DBAPI 错误）时抛 ExportConflictError，最终路径不存在、无 `.altero-export-*` 残留。证据：故障注入测试。
  - `rule` TR-2.3: 归档中七份 JSON 文档结构与现有 round-trip 测试完全兼容（现有 TestRoundTrip 不改断言通过）。证据：现有 tests/test_transfer.py。
- **Notes**: 隔离级别必须在会话第一条语句前设置；因此 Web 路由将使用独立新会话（见 Task 7），CLI 会话在命令开始时尚无查询。
- **Completion Evidence**:
  - TR-2.1 pass：test_export_reads_the_old_snapshot_while_a_write_commits —— 导出会话第一条语句后挂起，他会话提交写入，归档 manifest 版本与 items 仍等于边界版本。
  - TR-2.2 pass：test_a_serialization_failure_aborts_the_export（SQLSTATE 40001 DBAPIError 注入）→ ExportConflictError，目标不存在、无工作区残留。
  - TR-2.3 pass：现有 TestRoundTrip 及全部既有测试不改断言通过（31 passed in tests/test_transfer.py）。

## Task 3: 工作区状态机、校验与原子发布
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 2
- **Description**:
  - 在 `destination.parent` 下创建唯一工作区目录 `.altero-export-<token>/`（含 token，天然区分并发导出），内含 sidecar `export.json`（started_at、库 type/owner、边界版本、状态 writing/validating、pid 等）与半成品 `archive.part`。
  - ZIP 写入 `archive.part`；manifest 最后写入（manifest 存在即结构完整的自然标记）。
  - 关闭后重新打开校验：`testzip()` 全部 CRC、成员集合恰为 manifest + 七份文档 + 0..n 个 files/<digest>、manifest counts 与各文档长度及附件记录一致。
  - 校验通过后 `os.replace(archive.part, destination)` 原子发布并 fsync 目标目录；成功后删除工作区。
  - 任意阶段失败：区分错误类型（写入失败→ExportWriteError；发布失败→ExportPublishError；中止类保留原异常），记录日志，清理整个工作区；destination 已存在时保持原状、不存在时不产生。
- **Acceptance Criteria Addressed**: AC-4, AC-5, AC-6, AC-10, AC-16
- **Test Requirements**:
  - `rule` TR-3.1: 成功导出仅在 destination 留有完整归档，目标父目录无 `.altero-export-*`；归档可通过 testzip 且 manifest 为最后写入成员。证据：测试。
  - `rule` TR-3.2: 注入 ZIP 写/flush 阶段 OSError → ExportWriteError；注入 os.replace OSError → ExportPublishError；两种情况下 destination 不存在且父目录无工作区残留。证据：故障注入测试。
  - `rule` TR-3.3: 空库导出成功：counts 全 0、无 files 成员，导出文件可导入空目标库且仍为空。证据：空库往返测试。
  - `rubric` TR-3.4: 清理完整性维度；1-5；锚点 1=失败后残留工作区，3=主路径清理但个别异常路径残留，5=全部成功/失败路径均无残留；阈值 >=4；证据：逐路径列举目标父目录文件。
- **Notes**: sidecar 主要服务于重启回收的状态与时间判定（Task 6），不参与发布正确性。
- **Completion Evidence**:
  - TR-3.1 pass：test_only_the_published_archive_remains_and_manifest_is_last（destination 独存、无工作区、namelist[-1]==manifest.json、testzip None）。
  - TR-3.2 pass：test_a_write_failure_aborts_and_cleans（FailingZip ENOSPC → ExportWriteError）；test_a_publish_failure_aborts_and_cleans（os.replace EIO → ExportPublishError）；两路径 destination 不存在、无残留。
  - TR-3.3 pass：test_an_empty_library_exports_and_returns_empty —— counts 全 0、成员恰为 manifest+七文档、导入 target 后 is_empty 为真。
  - TR-3.4 rubric：得分 5/5。理由：成功路径仅留最终归档；写入失败、发布失败、附件中止、快照冲突四类路径均断言父目录无工作区/半成品；依据假设中的 digest 路径不可变约定与逐路径列举证据。

## Task 4: 附件导出边界与 manifest 附件记录
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 3
- **Description**:
  - 边界时 backing 文件存在的 digest 集合为本次附件集合（边界时无 backing 的 digest 维持不纳入、非错误）。
  - 附件以流式（shutil.copyfileobj，分块）写入 `archive.part` 的 `files/<digest>`，写入时计算 size 与 sha256；写前记录源文件 `(size, mtime_ns)`。
  - 全部附件写入后、发布前逐个复读源文件 stat：消失 → AttachmentMissingError；size 或 mtime_ns 与记录不符 → AttachmentChangedError。
  - manifest 新增可选字段：`exportedAt`（ISO 时间）与 `files`: [{"digest","size","sha256"}]；counts.files 保持按 digest 计数；format 仍为 1。
- **Acceptance Criteria Addressed**: AC-3, AC-11, AC-15
- **Test Requirements**:
  - `rule` TR-4.1: 边界内附件在读取前被删除 → AttachmentMissingError；在写入后、最终校验前被替换（内容/mtime 改变）→ AttachmentChangedError；均中止、清理工作区、destination 不存在。证据：故障注入测试。
  - `rule` TR-4.2: 两个附件共享同一 md5 时归档仅一个 files/<digest> 成员，manifest.files 中仅一条记录，counts.files 为 1；附件 sha256/size 记录与实际内容一致。证据：去重测试。
  - `rule` TR-4.3: 边界时本就缺失 backing 的 digest 不导致错误，归档与 counts 行为同现状。证据：测试。
  - `rubric` TR-4.4: 可诊断性维度（附件两类错误的 message/日志含库标识与 digest）；1-5；锚点按 AC-15；阈值 >=4；证据：测试断言与代码审查。
- **Notes**: 不重读源文件做二次全量哈希；基于"digest 路径不可变"约定，以 size+mtime_ns 判定替换，sha256 作为归档产物记录。
- **Completion Evidence**:
  - TR-4.1 pass：test_a_attachment_that_disappears_aborts_the_export（deleted.json 写入后删源文件 → AttachmentMissingError，digest/message 正确）；test_an_attachment_replaced_during_the_export_aborts（manifest 写入后替换源文件 → AttachmentChangedError）。
  - TR-4.2 pass：test_shared_digests_are_one_file_with_a_correct_record —— 两个附件同一 md5，files 成员仅一、manifest.files 一条、counts.files=1、sha256/size 与实际一致。
  - TR-4.3 pass：test_a_digest_with_no_backing_file_is_not_an_error —— 导出成功，counts.files=0、files=[]。
  - TR-4.4 rubric：得分 5/5。理由：附件两类错误独立异常，message 含库 type/owner 与 digest（"disappeared"/"replaced" 及阶段说明），logger 记录导出成功事件含库与目标路径；运维可直接定位。

## Task 5: 导入侧兼容新增 manifest 字段
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 4
- **Description**:
  - import_library/read_manifest 对新增字段全部按可选处理：存在 manifest.files 时，校验归档 files/ 成员与清单 digest 集合一致（成员缺失或多出均以 InvalidInputError 拒绝）；不存在时从 namelist 推导（旧归档行为）。
  - 文件落存储语义不变（仍不比对字节与 digest 名、仍整文件写入）。
- **Acceptance Criteria Addressed**: AC-12
- **Test Requirements**:
  - `rule` TR-5.1: 手工去除 manifest 中 exportedAt/files 的旧风格归档可正常导入且内容正确。证据：兼容性测试。
  - `rule` TR-5.2: 新归档（含 files 清单）正常导入；files 清单与实际成员不一致的篡改归档被 InvalidInputError 拒绝。证据：测试。
  - `rule` TR-5.3: 未来格式（format>1）与非 ZIP 拒绝行为保持现状。证据：现有测试保持通过。
- **Completion Evidence**:
  - TR-5.1 pass：test_an_archive_without_the_new_manifest_fields_imports —— 去除 files/exportedAt 的旧风格归档导入成功（版本 37、非空）。
  - TR-5.2 pass：test_a_manifest_naming_a_missing_file_is_refused（清单多出不存在 digest → "do not match"）；test_a_malformed_files_record_is_refused（files 非 list → "malformed"）。
  - TR-5.3 pass：future format / non-zip 现有拒绝测试保持通过。

## Task 6: 未发布工作区回收：服务函数、启动钩子、CLI 命令
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 3
- **Description**:
  - transfer.py 新增 `recover_workspaces(root, *, grace) -> int`：扫描 root 下 `.altero-export-*` 目录，依据 sidecar started_at（缺失则用目录 mtime）判定，超过 grace 的整个删除并计数；不触碰任何其他文件（含已发布 .zip）；单个目录删除失败只警告不影响其余。
  - app.py lifespan 启动时以固定宽限期常量对 settings.export_path 执行一次回收，异常捕获并记录日志，不阻断启动。
  - cli.py 在 `library` 下新增 `recover-exports [path ...] [--grace minutes]`：默认 path=settings.export_path，打印回收数量。
- **Acceptance Criteria Addressed**: AC-7, AC-8, AC-14
- **Test Requirements**:
  - `rule` TR-6.1: 旧工作区（started_at 超 grace）被删除、计数正确；新工作区保留；已发布 .zip（含 export_path 根下的已发布文件）始终存在且内容不变。证据：回收测试。
  - `rule` TR-6.2: 回收中某个目录删除失败时其余仍被处理；启动钩子回收抛异常时应用可正常启动并产生日志。证据：故障注入测试。
  - `rule` TR-6.3: CLI 命令处理函数对指定目录执行回收并报告数量，无参数时使用 export_path。证据：直接调用处理函数的测试。
- **Completion Evidence**:
  - TR-6.1 pass：test_old_removed_young_kept_confirmed_archive_preserved（旧删、新留、已发布 zip 内容不变）；sidecar 缺失回退目录 mtime 用例通过。
  - TR-6.2 pass：test_one_failed_removal_does_not_stop_the_rest（rmtree 故障隔离）；test_startup_recovery_failure_does_not_block_start（lifespan 正常进出并记录 "recovery failed"）。
  - TR-6.3 pass：test_the_cli_command_reports_reclaimed_workspaces（指定路径）；test_the_cli_command_without_paths_uses_the_export_directory（默认 export_path）。

## Task 7: Web 下载路由改造
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 4, Task 6
- **Description**:
  - download_archive：权限检查保持在请求会话；导出本身使用 session_factory 的全新会话（满足隔离级别须先于任何语句设置）。
  - destination 为 settings.export_path 下带唯一 token 的最终文件名（如下载展示名保持 altero-<type>-<owner>.zip）；export_path 自动创建。
  - 仅在发布成功后返回 FileResponse；background 任务删除该已发布文件（工作区已由导出流程自行清理）。
  - 导出失败时由全局异常处理返回错误响应，export_path 中无工作区、无半成品残留。
- **Acceptance Criteria Addressed**: AC-9, AC-13
- **Test Requirements**:
  - `rule` TR-7.1: 现有 TestExport/权限测试全部不改断言语义通过（含成员/管理员/401/404）。证据：现有 test_web_transfer.py。
  - `rule` TR-7.2: 成功下载的归档内容正确，响应结束后 export_path 无残留；故障注入（附件消失）返回 409 JSON，export_path 无工作区与半成品。证据：新增 Web 测试。
  - `rule` TR-7.3: 导出工作区文件名不出现在任何可路由 URL（404），归档响应头 content-disposition 保持既有文件名。证据：测试。
- **Completion Evidence**:
  - TR-7.1 pass：原有 TestExport 权限/功能测试全部不改断言通过（18 passed）。
  - TR-7.2 pass：test_a_failed_export_returns_409_and_leaves_nothing（附件消失注入 → 409 JSON 含 disappeared，export 目录为空）；test_the_published_file_is_removed_after_the_download（响应后发布文件已删）。
  - TR-7.3 pass：test_export_workspace_files_are_not_reachable_as_urls（/exports/… → 404）；content-disposition 仍为 altero-user-1.zip（原有用例覆盖）。

## Task 8: 端到端故障矩阵与全量回归
- **Status**: `completed`
- **Priority**: medium
- **Depends On**: Task 5, Task 6, Task 7
- **Description**:
  - 汇总端到端矩阵：空库、共享 digest、并发写入稳定快照、附件消失/变更、写入失败、发布失败、重启回收、已发布归档保护、旧归档导入，逐项在真实 app + 文件系统上验证。
  - 运行全量测试套件（pytest），确认无回归、无工作区污染；检查 GetDiagnostics 无新增诊断问题。
- **Acceptance Criteria Addressed**: AC-1..AC-16（回归证据汇总）
- **Test Requirements**:
  - `rule` TR-8.1: 全量 pytest 通过（含 compatibility_tests 中与归档相关用例如可运行）。证据：命令输出。
  - `rubric` TR-8.2: 整体工程质量维度（分层合规、无调试残留、日志与异常一致）；1-5；3=功能完整但存在风格/分层瑕疵，5=与现有代码风格完全一致且无多余文件；阈值 >=4；证据：测试套件 + 代码审查。
- **Completion Evidence**:
  - TR-8.1 pass：`uv run pytest tests/ -q` → 2760 passed, 18 skipped, 1 warning in 482.73s，EXIT:0；仓库根无 exports/storage/.altero 残留。
  - TR-8.2 rubric：得分 5/5。理由：新增代码遵循现有分层（services 无 web 框架导入，test_architecture 通过）；无调试残留、无多余文件；异常体系、日志与现有风格一致；注释沿用项目解释性 docstring 风格。

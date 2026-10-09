# 孤儿附件清理的删除租约仲裁 - 实施计划

## Task 1: 新增 StorageDeleteLease 模型与 Alembic 迁移
- **Status**: `completed`
- **Priority**: high
- **Depends On**: None
- **Description**:
  - 在 [models/storage.py](file:///Users/ding/Documents/swe/0910091/project-03/src/altero/models/storage.py) 新增 `StorageDeleteLease` 模型（表名 `storage_delete_leases`）：`digest` String(32) 主键；`status` String(16) 非空（`candidate`/`deleted`，默认 candidate）；`size` BigInteger；`mtime_ns` BigInteger；`available_at` DateTime（索引或复合索引）；`attempts` Integer 默认 0；`last_error` String(255) 可空；`retry_after` DateTime 可空；`created`/`updated`（server_default now）、`deleted_at` 可空。复合索引 `(status, available_at)` 供裁决查询。
  - 在 [models/__init__.py](file:///Users/ding/Documents/swe/0910091/project-03/src/altero/models/__init__.py) 导出并加入 `__all__`。
  - 新增 Alembic 迁移（down_revision = `d7cd57abc8a4`，batch 模式兼容 SQLite）：upgrade 建表+索引，downgrade 反向清理。
- **Acceptance Criteria Addressed**: AC-1, AC-12
- **Test Requirements**:
  - `rule` TR-1.1: `Base.metadata.create_all` 建出的表可对 StorageDeleteLease 做 insert/select（经现有 session fixture 的全量测试间接验证）。
  - `rule` TR-1.2: `alembic upgrade head` 成功且再 `downgard -1` 成功，无残留表；证据为命令输出。
  - `rule` TR-1.3: 迁移脚本风格与既有迁移一致（revision/down_revision 链正确、batch_alter_table 建索引）；证据为代码评审。
- **Completion Evidence**:
  - 模型 [storage.py](file:///Users/ding/Documents/swe/0910091/project-03/src/altero/models/storage.py#L80-L133) 已加 StorageDeleteLease（11 列 + 复合索引），models/__init__.py 已导出。
  - TR-1.2: `ALTERO_DATABASE_URL=... uv run alembic upgrade head` 迁移至 cea5b5e75cbe，DDL 含表与 ix_storage_delete_leases_due；`downgrade -1` 后 sqlite_master 无残留（命令已实测）。
  - TR-1.3: 迁移 [cea5b5e75cbe](file:///Users/ding/Documents/swe/0910091/project-03/migrations/versions/cea5b5e75cbe_hold_a_deletion_lease_for_each_orphan_.py) 沿用 (CURRENT_TIMESTAMP)/batch_alter_table 既有风格，down_revision 链接 d7cd57abc8a4。

## Task 2: 租约仲裁辅助函数与上传/注册路径接入
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 1
- **Description**:
  - 在 [services/storage.py](file:///Users/ding/Documents/swe/0910091/project-03/src/altero/services/storage.py) 增加租约仲裁辅助（内部函数，名称实现时定）：
    - `async def revoke_delete_lease(session, md5)`：在调用方事务内对该 digest 的 candidate 租约执行加锁删除（先 `SELECT ... FOR UPDATE` 再删，或等效的带锁 DELETE），用于上传取得所有权；`FOR UPDATE` 需遵循 SQLite 被忽略、PG 生效的项目惯例并注释。
    - `async def claim_for_upload(session, root, declared) -> bool`：供 `authorize` 使用——先读磁盘；文件存在则撤销候选租约并**持锁后复查磁盘**，返回文件是否仍可用；文件缺失时清理同 digest 残留候选租约并返回 False。
  - 改造 `authorize`：以 `claim_for_upload` 取代裸 `file_path(...).is_file()` 分支判断；返回 True→原 `attach` + `{"exists": 1}` 逻辑不变；False→既有的创建 StorageUpload 流程不变（创建 upload 行前确保残留 candidate 已清理）。
  - 改造 `register`：在 `_confirm_stored_file` 通过后、attach/upload 删除的同一事务中撤销同 digest candidate 租约（防御性，覆盖获取快照后新出现的租约）。
  - 不改变任何返回体形状、版本号处理与异常类型。
- **Acceptance Criteria Addressed**: AC-2, AC-3, AC-4, AC-10, AC-11, AC-13
- **Test Requirements**:
  - `rule` TR-2.1: candidate 租约 + 磁盘有文件时，`authorize` 返回 `{"exists": 1}`、租约被删除、item 挂载 md5；证据为服务层单测。
  - `rule` TR-2.2: candidate 租约 + 磁盘无文件时，`authorize` 返回上传指引并创建 StorageUpload；随后完整 bytes+register 流程成功，文件在盘、item 已挂载；证据为服务层/API 测试。
  - `rule` TR-2.3: `register` 成功后同 digest 不存在 candidate 租约；证据为数据库断言。
  - `rule` TR-2.4: 既有 tests/test_files.py 全部不修改通过；证据为 pytest 输出。
  - `rubric` TR-2.5: 锁顺序与崩溃方向可核查性；尺度 1-5；锚点 1=存在新阻塞/无锁窗口，3=主路径正确但一处含糊，5=逐行可核查且 docstring 说明 why；阈值 >=4；证据为代码评审。
- **Completion Evidence**:
  - [storage.py](file:///Users/ding/Documents/swe/0910091/project-03/src/altero/services/storage.py#L267-L329) 新增 `revoke_delete_lease`（FOR UPDATE 后删除，含 SQLite/PG 注释）与 `claim_for_upload`（先读盘→持锁撤销→再读盘）；[authorize](file:///Users/ding/Documents/swe/0910091/project-03/src/altero/services/storage.py#L352-L358) 接入；[register](file:///Users/ding/Documents/swe/0910091/project-03/src/altero/services/storage.py#L559-L565) 在删除 upload 行前撤销租约（两侧锁序均为"租约→upload 行"，避免 PG 死锁）。
  - TR-2.1/2.2/2.3: 由 `TestUploadsArbitrateWithTheLease`、`TestCrashesAndRetries.test_a_crashed_purge_never_blocks_a_new_upload` 与 API 级 `TestOrphanPurgeArbitration` 覆盖，全部通过。
  - TR-2.4: tests/test_files.py 53 项全绿（零修改既有测试）。

## Task 3: purge_orphans 两阶段租约获取与裁决改造
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 1
- **Description**:
  - 改造 [services/storagestats.py](file:///Users/ding/Documents/swe/0910091/project-03/src/altero/services/storagestats.py) 的 `purge_orphans`，保持签名 `(session, root, *, grace) -> tuple[int,int]` 与 staging 清理段不变：
    1. **终态清理**：删除超过可复核窗口（deleted_at 早于 now-grace）的 `deleted` 租约；对磁盘上重新出现的终态 digest，允许其按当前身份重新进入候选（实现方式：终态行先删后插，或显式复活）。
    2. **候选获取（独立事务提交）**：沿用 scan_store + mtime cutoff 得旧文件；一次性取得引用并集（`_digests_by_library`）、在途上传 digest 集合（storage_uploads）、未过期下载 digest 集合（storage_downloads.expires >= now）；对旧且无三者所有、且无 candidate 租约的文件，按身份快照（size、mtime_ns、available_at=mtime+grace、now）upsert candidate 行并提交。
    3. **逐候选裁决（每候选独立事务，FOR UPDATE 锁租约行）**：仅处理 `status=candidate AND now>=available_at AND (retry_after IS NULL OR now>=retry_after)`；事务内重新查询：引用并集、`SELECT ... FOR UPDATE` 锁 storage_uploads 匹配行、`FOR UPDATE` 锁未过期 storage_downloads 匹配行；重新 stat 磁盘。
       - 出现任一所有者 → 保留候选（不改快照），提交，继续下一候选；
       - 磁盘缺失 → 置 status=deleted/deleted_at=now 收敛，freed 不增加；
       - size 或 mtime_ns 与快照不一致 → 删除租约（释放），不删文件；
       - 全部通过 → 事务内 unlink；OSError → attempts+1、last_error（截断 255）、retry_after=now+退避（指数、上限 grace），文件保留、freed 不增加；成功 → status=deleted/deleted_at=now，removed+1、freed+=快照 size，提交。
  - `collect`/`scan_store`/`_sweep_staging`/Usage 数据结构与 webadmin 返回体不做任何改动。
  - 更新函数 docstring 描述两阶段、仲裁矩阵与崩溃收敛。
- **Acceptance Criteria Addressed**: AC-1, AC-4, AC-5, AC-6, AC-7, AC-8, AC-9, AC-10, AC-11, AC-13
- **Test Requirements**:
  - `rule` TR-3.1: 旧孤儿单次调用被删且留下 deleted 租约，(removed, freed) 正确；新文件、被引用文件、共享 digest 文件行为同既有测试。
  - `rule` TR-3.2: 存在 StorageUpload 行 / 未过期 StorageDownload 行 / item 引用（含他库与回收站）时裁决保留候选且不计数。
  - `rule` TR-3.3: 身份变化（os.utime 或 store_file 重落）→ 租约释放、文件保留、本次 (0,0)，年轻文件不被立即重新候选。
  - `rule` TR-3.4: monkeypatch unlink 抛 OSError → attempts/last_error/retry_after 落库，退避期内不重试；解除后过退避期成功删除。
  - `rule` TR-3.5: candidate + 文件已缺失 → 收敛 deleted 且 freed=0；再次 purge 为空操作 (0,0)。
  - `rule` TR-3.6: deleted 租约在窗口后被清理；窗口内重试不重复计数。
  - `rule` TR-3.7: staging 清理计数与 collect 全部统计字段与改动前完全一致（既有测试不改通过）。
  - `rubric` TR-3.8: 裁决事务隔离级别下的锁正确性（upload/download 行锁 + 租约行锁）与"偏向保留"失败方向；尺度 1-5；阈值 >=4；证据为逐行评审与交错测试。
- **Completion Evidence**:
  - [storagestats.py](file:///Users/ding/Documents/swe/0910091/project-03/src/altero/services/storagestats.py#L202-L493) 实现 `_prune_terminal`、`_acquire_candidates`（独立事务、available_at=mtime+grace）、`_locked_owner_exists`（FOR UPDATE）、`_decide_candidate`（五分支 removed/retained/released/converged/failed，每候选独立事务）与重写的 `purge_orphans`；collect/scan_store/Usage/webadmin 零改动。
  - TR-3.1~3.7: 由 `TestTheLeaseAnOrphanIsDeletedUnder`、`TestUnlinkThatFails`、`TestCrashesAndRetries` 与既有 `TestPurgingOrphans`/`TestFilesLeftHalfWritten` 全部覆盖通过；freed 仅在 confirmed removed 时累加。
  - TR-3.8: 锁序（租约行→owner 行）、populate_existing 取锁、每分支提交/回滚边界已在代码与 docstring 中逐行说明，留待独立评审复核。

## Task 4: 服务层竞态交错与崩溃收敛测试
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 2, Task 3
- **Description**:
  - 在 tests/test_storage_stats.py 与 tests/test_files.py 中新增测试（优先追加到既有文件，沿用其 fixture/helper 风格；不修改既有测试）：
    - 复现报告中的竞态：旧孤儿→候选租约后 authorize 即时挂载→租约撤销、随后 purge 不删（AC-2）。
    - 裁决先行删除、authorize 后到→退回上传三步并成功注册（AC-3）。
    - 在途 upload 行与 register 后引用两个窗口（AC-4）。
    - 下载授权未过期/过期（AC-5）；他库/回收站引用（AC-6）。
    - 身份变化释放（AC-7）；unlink OSError 退避矩阵（AC-8）；unlink 后回滚崩溃收敛 + 管理员重试 + 新上传不阻塞（AC-9、AC-10）。
    - 确定性交错：使用两个 session/直接 service 调用按序 await 复现"获取租约→上传插入/提交→持锁复核"顺序；不依赖真实线程时序。
  - 覆盖 webadmin `/storage/purge` 返回体形状不回归（如需要追加一条 API 级测试）。
- **Acceptance Criteria Addressed**: AC-2, AC-3, AC-4, AC-5, AC-6, AC-7, AC-8, AC-9, AC-10
- **Test Requirements**:
  - `rule` TR-4.1: 上述每个场景均有独立测试且全绿；证据为 pytest -v 输出。
  - `rule` TR-4.2: 测试不依赖 sleep/真实并发时序（确定性交错），无 flaky；证据为重复运行 3 次全绿。
- **Completion Evidence**:
  - 新增 14 个测试：[test_storage_stats.py](file:///Users/ding/Documents/swe/0910091/project-03/tests/test_storage_stats.py#L291-L748)（13 个：终态记录、到期门、upload/download/回收站引用保留、身份释放、unlink 退避矩阵、崩溃收敛不冒领字节、残留不阻塞新上传、终态修剪/复活、authorize 撤销与回退三步）+ [test_files.py](file:///Users/ding/Documents/swe/0910091/project-03/tests/test_files.py#L943-L1006)（1 个 HTTP 端到端竞态回归）。
  - TR-4.1: 全部通过（两文件 83 项）。
  - TR-4.2: 新测试子集连续 3 次运行均 15 passed，无 sleep/线程时序。

## Task 5: 全量回归、静态检查与迁移验证
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 2, Task 3, Task 4
- **Description**:
  - 运行全量测试套件（pytest）、ruff check（必要时 ruff format），确认无警告新增。
  - 运行 alembic upgrade head / downgrade -1 验证迁移；确认测试库 create_all 路径可用。
  - 检查 compatibility_tests 中附件生命周期相关用例不被破坏（至少静态核对 storage 协议返回形状）。
- **Acceptance Criteria Addressed**: AC-11, AC-12, AC-13
- **Test Requirements**:
  - `rule` TR-5.1: `pytest` 全量通过（既有测试零修改）；证据为命令输出。
  - `rule` TR-5.2: `ruff check` 零错误；证据为命令输出。
  - `rule` TR-5.3: alembic 升降级成功；证据为命令输出。
  - `rubric` TR-5.4: 变更最小性——共享 digest、staging、统计口径、缺失报告、下载行为零改动可被 diff 证实；尺度 1-5；阈值 >=4；证据为 diff 评审。
- **Completion Evidence**:
  - TR-5.1: 全量 `uv run pytest -q`：2748 passed, 18 skipped（唯一 warning 为既存 starlette/httpx 弃用，与本次无关）。
  - TR-5.2: `uv run ruff check .` 零错误，`ruff format --check .` 420 文件全部合规；`uv run ty check` 通过。
  - TR-5.3: SQLite 上 alembic upgrade→downgrade -2→upgrade head 往返成功；create_all 经全量测试验证。
  - TR-5.4: 生产代码 diff 限定于 models/storage.py、services/storage.py、services/storagestats.py、models/__init__.py 与一个新迁移；files.py 路由、webadmin.py、collect/scan_store/Usage、transfer.py 零改动。

## Issue I-1: SQLite/WAL 下裁决事务 unlink 前无写锁，竞态仍致数据丢失（R1 F1）
- **Status**: `completed`
- **Priority**: high
- **Depends On**: None
- **Discovered By**: Review R1
- **Description**:
  - SQLite 忽略 FOR UPDATE；_decide_candidate 在 unlink 之前只有读语句，WAL 下并发 authorize 的 revoke+attach 可在其间提交；purge 随后 unlink 已引用文件（reviewer 探针实测 DATA LOSS，随后 StaleDataError/500）。
  - 修复：裁决事务在任何复核读取之前先执行写操作 claim 候选行（UPDATE storage_delete_leases SET updated=now WHERE digest=:d AND status='candidate'，rowcount=0 即跳过），事务起始先 rollback 结束外层枚举的读事务；revoke_delete_lease 改为单条 write-first DELETE（PG RC 下等待行锁并重判，SQLite 下首语句即取 RESERVED 写锁）。due 枚举改为纯标量行，避免 rollback 后 ORM 过期对象访问。
- **Acceptance Criteria Addressed**: AC-2, AC-13
- **Test Requirements**:
  - `rule` TR-I-1.1: 双 session 真并发交错（purge 持 claim 后放行 authorize）下，不出现"引用已提交但文件被删"：purge 先完成则 authorize 收敛为三步上传、最终文件可用；回归探针输出无 DATA LOSS。
  - `rule` TR-I-1.2: 既有 2748 测试全量保持通过，ruff/ty 零错误。
- **Completion Evidence**:
  - [_decide_candidate](file:///Users/ding/Documents/swe/0910091/project-03/src/altero/services/storagestats.py#L351-L406) 重写：入口 pending-free `commit()` 结束枚举读事务（expire_on_commit=False，不过期调用方对象）→ 首语句 UPDATE claim（PG 行锁 / SQLite writer lock）→ rowcount=0 跳过 → 持锁复核；due 枚举改为标量元组。[revoke_delete_lease](file:///Users/ding/Documents/swe/0910091/project-03/src/altero/services/storage.py#L267-L306) 改为 write-first DELETE（docstring 含 BUSY_SNAPSHOT 失败方向说明）。
  - TR-I-1.1: `TestRealConcurrentArbitration` 双连接 + 事件闸门 + shield/timeout 判别（attach 不能在 purge 持 writer lock 时提交）；移除 claim 后该测试确定性失败（实测 pytest.fail: committed through a decision that held no write lock），恢复后连续 4 次通过。
  - TR-I-1.2: 全量 2752 passed / 18 skipped；ruff、ty 全绿。

## Issue I-2: 通用 item 写入引入 md5 时不经租约仲裁（R1 F2）
- **Status**: `completed`
- **Priority**: `medium`
- **Depends On**: I-1
- **Discovered By**: Review R1
- **Description**:
  - itemwrites._apply 直接持久化客户端 md5 字段（同步通用写入/复制/导入路径），其事务只持 library 锁，不碰租约行；PG RC 下该引用插入与 purge unlink 仍可交错。
  - 修复：_apply flush 后，对 item 当前所有非空 md5 字段在同一事务内执行 revoke_delete_lease（与 register 相同语义；无循环导入风险需核查）。
- **Acceptance Criteria Addressed**: AC-2, AC-13
- **Test Requirements**:
  - `rule` TR-I-2.1: 通用写入携带 md5 提交后，同 digest candidate 租约被撤销、随后 purge 不删文件；既有 item 写入测试零回归。
- **Completion Evidence**:
  - [itemwrites._apply](file:///Users/ding/Documents/swe/0910091/project-03/src/altero/services/itemwrites.py#L440-L451) flush 后对 item 的非空 md5 集合执行 storage.revoke_delete_lease（已核查无循环导入：storage 仅依赖 errors/keys/models）。
  - TR-I-2.1: `TestWritesOutsideTheFileProtocol.test_a_generic_item_write_revokes_a_candidate` 经 itemwrites.save_item 验证撤销+purge 不删；tests/test_items_write.py、test_objects_write.py 及全量套件零回归。

## Issue I-3: 获取阶段 stat 竞态与并发 purge 主键冲突加固（R1 F3/F4，advisory）
- **Status**: `completed`
- **Priority**: low
- **Depends On**: I-1
- **Discovered By**: Review R1
- **Description**:
  - _acquire_candidates 逐文件 stat 捕获 OSError 跳过（文件在扫描后消失不应中断整次 purge）；候选 INSERT 提交遇 IntegrityError（另一 purge 并发获取）时回滚并继续裁决（赢家插入的候选本运行同样会裁决）。
- **Acceptance Criteria Addressed**: AC-13
- **Test Requirements**:
  - `rule` TR-I-3.1: stat 抛 OSError 时 purge 正常处理其余候选；两个 purge 并发不产生未捕获异常（以 monkeypatch/双任务验证）。
- **Completion Evidence**:
  - [_acquire_candidates](file:///Users/ding/Documents/swe/0910091/project-03/src/altero/services/storagestats.py#L301-L324) stat 包 try/except OSError 跳过；commit 捕获 IntegrityError → rollback。
  - TR-I-3.1: `TestAcquisitionIsBestEffort` 两个测试覆盖（消失文件不建行；IntegrityError 后回滚且下次 purge 正常删除）。

## Issue I-4: 补双 session 真并发交错回归（R1 F5，advisory）
- **Status**: `completed`
- **Priority**: medium
- **Depends On**: I-1, I-2
- **Discovered By**: Review R1
- **Description**:
  - 在 tests/test_storage_stats.py 增加使用 app.state.database 第二 session + 事件闸门的确定性并发测试：purge 裁决持 claim 后挂起，authorize 在另一连接并发执行，验证两个方向的收敛；重复运行稳定。
- **Acceptance Criteria Addressed**: AC-2, AC-3, AC-13
- **Test Requirements**:
  - `rule` TR-I-4.1: 并发测试连续 5 次运行全绿、无 sleep 依赖时序（仅用事件闸门）。
- **Completion Evidence**:
  - [TestRealConcurrentArbitration](file:///Users/ding/Documents/swe/0910091/project-03/tests/test_storage_stats.py#L755-L868) 使用独立连接 + asyncio 事件闸门 + shield/1s 超时判别；无 sleep 时序依赖（1s 仅为锁阻塞判别窗口）。连续 5 次通过；移除 write-claim 后确定性失败（牙齿验证实测）。

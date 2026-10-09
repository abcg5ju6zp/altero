# 孤儿附件清理的删除租约仲裁 - 独立审查

- [x] CP-R1: 旧孤儿经租约两阶段在单次 purge 内删除并留下 deleted 终态
  - **Type**: `rule`
  - **Covers**: AC-1, TR-3.1
  - **Evidence**: R1 pass；R2 复核 5/5（旧孤儿测试+终态断言+全量 2752）

- [x] CP-R2: 引用查询后即时挂载（exists=1）的新上传不被误删（竞态关闭）
  - **Type**: `rule`
  - **Covers**: AC-2, TR-2.1, TR-4.1, TR-I-1.1
  - **Evidence**: R1 fail（SQLite 读事务漏洞 F1）→ I-1 修复（写优先 claim + write-first DELETE）；R2 在 SQLite 双连接与 PostgreSQL 16 双连接分别实测串行成立，并发测试拔牙齿验证确定性失败/通过，5/5

- [x] CP-R3: 清理先行提交后，并发上传按磁盘/租约状态收敛为正常三步发送
  - **Type**: `rule`
  - **Covers**: AC-3, AC-10, TR-2.2
  - **Evidence**: R1 pass；R2 在 PG/SQLite 均实测 purge 先提交后 authorize 走三步并注册成功，5/5

- [x] CP-R4: 在途 upload 行、register 后引用、未过期 download 授权、他库/回收站引用均保留候选
  - **Type**: `rule`
  - **Covers**: AC-4, AC-5, AC-6, TR-3.2
  - **Evidence**: R1 pass；R2 五类所有者（含他库/回收站）复核通过，5/5

- [x] CP-R5: 文件身份（size/mtime_ns）变化释放租约且新身份重计宽限
  - **Type**: `rule`
  - **Covers**: AC-7, TR-3.3
  - **Evidence**: R1/R2 pass；R2 另以 PG 探针证实 transfer restore 的新 mtime 被身份闸门拦截，5/5

- [x] CP-R6: unlink OSError 记录 attempts/last_error/retry_after 退避，退避期内不重试
  - **Type**: `rule`
  - **Covers**: AC-8, TR-3.4
  - **Evidence**: R1/R2 pass，5/5

- [x] CP-R7: unlink 后提交前崩溃 / 响应丢失重试：收敛 deleted、freed=0、不重复计数
  - **Type**: `rule`
  - **Covers**: AC-9, TR-3.5
  - **Evidence**: R1/R2 pass（仅 confirmed unlink 计 lease.size），5/5

- [x] CP-R8: 终态租约窗口后修剪；文件在终态后回归可重新候选，不永久逃逸
  - **Type**: `rule`
  - **Covers**: AC-9（FR-9）, TR-3.6
  - **Evidence**: R1/R2 pass；R2 采纳 advisory-4 修正了 _prune_terminal docstring 措辞（实现无差异），5/5

- [x] CP-R9: 共享 digest、staging 清理、collect 统计口径、missing 报告、下载/查看行为零回归
  - **Type**: `rule`
  - **Covers**: AC-11, TR-3.7, TR-2.4, TR-5.1
  - **Evidence**: R1/R2 pass；collect/scan_store/_sweep_staging/Usage/files 下载路由零 diff；全量 2752 passed/18 skipped，5/5

- [x] CP-R10: 模型与 Alembic 迁移一致，upgrade/downgrade 干净，create_all 兼容
  - **Type**: `rule`
  - **Covers**: AC-12, TR-1.1, TR-1.2, TR-5.3
  - **Evidence**: R1/R2 均独立实测 upgrade head/downgrade -1 干净，5/5

- [x] CP-U1: 仲裁锁设计严密性（锁顺序、崩溃方向偏向保留、收敛矩阵完整、docstring 可逐行核查、失败方向安全）
  - **Type**: `rubric`
  - **Covers**: AC-13, TR-2.5, TR-3.8, TR-5.4
  - **Scale**: 1-5
  - **Anchors**: 1 = 仍有无保护 unlink 窗口、死锁可能或永久阻塞点；3 = 主竞态关闭但锁序/崩溃/计量有一处含糊；5 = 每个交错均有确定锁序与收敛路径，未知状态绝不计入 freed，逐行可核查
  - **Pass Threshold**: >= 4
  - **Evidence**: R1 3/5（F1/F2/F5）→ 修复后 R2 4/5 pass：双引擎锁序逐行核查（purge: lease→owner rows，不持 library；register: library→lease→upload；itemwrites: library→lease，无环）；BUSY_SNAPSHOT 失败方向安全；扣 1 分：transfer 旁路保护依赖"store_file 新 mtime"隐式不变量且暂无专属回归测试（advisory-2）

## Review History

### Review R1
- **Result**: `fail`
- **Evidence**: PG READ COMMITTED 锁序推理正确，CP-R1/R3-R10 pass；但 SQLite（项目默认后端、WAL）下裁决事务 unlink 前无任何写锁，reviewer 用真实 Database 双连接确定性交错实测：authorize exists=1 提交后 purge 仍 unlink，文件消失、引用已落库（StaleDataError 后 500），数据不可恢复。
- **Findings**:
  - F1（actionable/high）：SQLite/WAL 下目标竞态仍可致数据丢失；修复方向——裁决事务首语句改为写操作（UPDATE claim 候选行），使两后端都在持有写锁后做复核；revoke 侧改为 write-first DELETE。
  - F2（actionable/medium）：通用 item 写入（itemwrites._apply 持久化 md5 字段）不经租约仲裁，PG RC 下存在同类残余窗口；应在写事务内对写入的 md5 撤销候选租约。
  - F3（advisory/low）：候选获取阶段 per-file stat 无 OSError 防护，单文件竞态消失会中断整次 purge。
  - F4（advisory/low）：两个 purge 并发时 PG 上候选 INSERT 可能撞主键；应吞掉 IntegrityError 或冲突忽略。
  - F5（advisory/medium，测试有效性）：新测试均为顺序交错；需补双 session 真实并发交错回归。

### Review R2
- **Result**: `pass`
- **Evidence**:
  - 修复独立复核：I-1 写优先 claim（入口 pending-free commit 结束读事务 → 首语句 UPDATE 取 PG 行锁/SQLite writer lock → 持锁复核后 unlink）、I-2 itemwrites._apply 撤销租约、I-3 stat/IntegrityError 加固、I-4 双连接事件闸门并发测试（含 shield/timeout 锁阻塞判别器）。
  - Reviewer 独立实测：SQLite WAL 双连接（读穿透旧漏洞复现；claim 后 DELETE 阻塞→purge commit 后二次读盘收敛；BUSY_SNAPSHOT 0ms 失败且方向安全）；PostgreSQL 16 双连接（RC 等待后按最新行版本重判；TestRealConcurrentArbitration 在 PG 原样通过；18 个原生 PG 测试通过）；并发测试牙齿验证（短路 claim 后确定性失败于判别器）；连续 8 次运行稳定；alembic 往返；全量 2752 passed/18 skipped；ruff/ty 全绿。
  - 可达交错穷举核查（register 三步、itemwrites 通用写、transfer restore 新 mtime 身份闸门、retention 过期行、item 删除、共享 digest 并集）：未发现任何"引用已提交但文件被 unlink"路径；锁序无等待环。
- **Findings（全部 advisory，不阻塞）**:
  1. SQLite 下裁决持 writer lock 跨复核+unlink，慢挂载时并发写最多等 busy_timeout 5s 后可重试失败——正确修复的必然代价，与 db.py 既定 SQLite 立场一致。
  2. transfer restore 交错安全目前依赖 store_file 新 mtime 隐式不变量（已 PG 探针证实），建议未来补专属回归测试。
  3. PG 上多候选反序撤销理论死锁（abort 重试、方向安全、单 item 单 md5 几乎不可达）。
  4. _prune_terminal docstring 措辞过强——**已在本轮修复时采纳并改正**（行为无差异）。
- **终态**: 无 actionable finding；所有 rule checkpoint 通过，CP-U1 4/5 达阈值；满足 pass 条件。

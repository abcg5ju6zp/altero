# 孤儿附件清理的删除租约仲裁 - 产品需求文档

## Overview

- **Summary**: 为运维人员手动触发的孤儿附件清理（`purge_orphans`）引入可复核的**删除租约（deletion lease）**机制。候选 digest 必须先取得数据库中的租约行，经过宽限期与加锁复核，确认无任何库引用、无在途上传/下载所有权、且磁盘文件身份未变化，才允许 unlink；上传落盘与附件注册路径通过同一租约串行仲裁，可在候选阶段撤销删除。
- **Purpose**: 修复真实的数据丢失竞态：清理线程在数据库引用查询结束后、unlink 之前，某个旧 digest 被新上传（尤其是 `authorize` 的 `{"exists": 1}` 即时挂载路径）重新注册，清理仍直接 unlink，删掉刚恢复引用的文件。同时保证清理过程在进程崩溃、提交丢失、响应丢失、unlink 失败等中断后可收敛、可重试，且不把未知状态计为已释放空间。
- **Target Users**: 自托管实例的运维/管理员（Web 管理后台 Storage 页面 "purge" 按钮的调用者），以及与其并发上传/下载附件的普通用户。

## Goals

- 候选回收以**持久化、可复核、可撤销**的租约行为仲裁点，清理与上传注册之间通过数据库行锁（PostgreSQL `FOR UPDATE`；SQLite 写串行）建立确定顺序。
- 删除裁决的全部前置条件在**持有的同一个事务中复核**：库引用（全部 library 的并集，含回收站与共享 digest）、在途上传（`storage_uploads`）、未过期下载授权（`storage_downloads`）、磁盘文件身份（size + mtime_ns）。
- 上传路径在候选阶段取得同一 digest 所有权时**撤销租约并继续正常注册**；若清理已先提交删除，则上传按磁盘与租约状态收敛为"文件缺失，正常发起上传"，不产生永久阻塞。
- 崩溃/丢失场景按磁盘与租约状态收敛：unlink 已完成而事务提交或响应丢失时，重试不重复计数、不重复删除、不误删。
- 三类复核结果分别处理：引用复核失败→保留候选；文件身份变化→释放租约；unlink 失败→记录可重试清理（次数、错误、退避时间）。
- 进程异常退出后不遗留永久阻塞新上传的状态（事务回滚释放行锁；残留候选在下次运行时按磁盘状态收敛）。
- 未知状态绝不计入 `freed` 字节数；只有本次运行中确认由本服务 unlink 成功的文件才计数。

## Non-Goals

- 不改变共享 digest 的去重存储语义、nominal/real 统计口径、missing files 报告、暂存文件（`.altero-*` staging）清理逻辑。
- 不改变正常附件下载、在线查看、302 授权跳转及其 5 分钟有效期语义。
- 不把孤儿清理改为定时任务，不新增配置项；仍由管理员带密码确认手动触发，宽限期仍为 24 小时（`ORPHAN_GRACE`）。
- 不引入分布式锁或跨实例协调；沿用现有"单服务器 + 一个数据库"的部署假设（多实例部署本项目不支持）。
- 不改变文件以 MD5 内容寻址、`store_file` 原子替换落盘、ZIP 快照校验等既有协议。

## Background & Context

- 现状代码：
  - [storage.py](file:///Users/ding/Documents/swe/0910091/project-03/src/altero/services/storage.py)：`authorize` 在 digest 路径已存在时直接 `attach` 并返回 `{"exists": 1}`（第 276-280 行），无任何行级协调；`register` 删除 `storage_uploads` 行并挂载 item 字段；`purge_stale_uploads` 清理废弃授权。
  - [storagestats.py](file:///Users/ding/Documents/swe/0910091/project-03/src/altero/services/storagestats.py)：`purge_orphans`（第 179-226 行）先列旧文件、再查一次引用并集，随后**无锁直接 unlink**——竞态窗口就在引用查询与 unlink 之间。
  - [files.py](file:///Users/ding/Documents/swe/0910091/project-03/src/altero/api/routes/files.py)：三步上传（authorize / bytes / register），路由对库加行锁（`writes.lock_library`）；下载经 `authorize_download` 生成 5 分钟有效的 `storage_downloads` 行。
  - [webadmin.py](file:///Users/ding/Documents/swe/0910091/project-03/src/altero/api/routes/webadmin.py#L499-L522)：`POST /web/admin/storage/purge`，返回 `{"files": n, "bytes": n}`。
- 关键既有不变量：文件先落盘后提交库行；`store_file` 经临时名 + `os.replace` 原子落地，新落地必产生新的 mtime；`scan_store` 只统计 32 位十六进制命名的文件；staging 文件不被 `scan_store` 统计。
- `writes.lock_library` 已示范本项目的锁惯例：`with_for_update()` 在 PostgreSQL 生效、在 SQLite 被忽略（SQLite 写本身串行）。
- Alembic 迁移链当前 head 为 `d7cd57abc8a4`；测试经 `Base.metadata.create_all` 自动建表，生产经 Alembic 迁移。

## Functional Requirements

- **FR-1（租约模型）**：新增删除租约表（按 digest 主键），至少记录：状态（`candidate`/`deleted`）、候选时文件身份快照（size、mtime_ns）、候选可裁决时间 `available_at`、失败尝试次数、最近错误、重试退避时间 `retry_after`、创建/更新/删除时间。模型在 `altero.models` 导出，并提供 Alembic 迁移（含降级）。
- **FR-2（两阶段清理）**：`purge_orphans` 改为"先获取候选租约（独立事务提交）→ 再逐候选加锁裁决"两阶段。候选仅从超过宽限期（mtime 早于 now-grace）、且快照时无库引用、无在途上传、无未过期下载授权的文件中产生；`available_at = 文件 mtime + grace`，故已度过完整宽限期的旧孤儿在同一次运行中即可被裁决（保持现有单次调用行为）。
- **FR-3（持锁复核与删除）**：裁决在独立事务中对租约行 `SELECT ... FOR UPDATE`，并在同一事务内：重新查询库引用并集、对 `storage_uploads`/`storage_downloads` 做 `FOR UPDATE` 所有权检查、重新 stat 磁盘；全部通过后在事务内 unlink，成功后把租约置为 `deleted` 并提交。
- **FR-4（上传撤销/收敛）**：`authorize` 中 digest 已存在的即时挂载路径，必须先与租约仲裁（删除候选租约，持锁后复查磁盘）：文件仍在→撤销租约并正常 `attach` 返回 `exists`；文件已被清理删除→撤销/忽略残留租约并退回正常授权（创建 upload 行、返回上传指引），客户端可正常发送字节完成注册。发起新上传（文件缺失创建 upload 行）时同样清理同 digest 的残留候选租约。
- **FR-5（注册路径仲裁）**：`register` 提交（挂载字段 + 删除 upload 行）时，候选租约不得导致误删；裁决事务对 `storage_uploads` 行的 `FOR UPDATE` 查询与注册事务的删除互锁，候选在 upload 行存在或引用已出现时保留。
- **FR-6（下载所有权）**：裁决复核必须把未过期的 `storage_downloads` 行视为在途下载所有权；有则保留候选，且不改变下载授权与下载响应的任何既有行为。
- **FR-7（三种复核结果）**：
  - 引用/上传/下载复核失败（出现所有者）→ 候选原样保留，下次运行重新复核；
  - 文件身份变化（size 或 mtime_ns 与快照不一致）→ 释放（删除）租约；该文件按新 mtime 重新计宽限、未来重新成为候选；
  - unlink 抛出 `OSError` → 不删除文件、不计数，记录 attempts+1、last_error 与 retry_after 退避，在退避结束前的后续运行不再裁决该候选。
- **FR-8（崩溃与重试收敛）**：unlink 成功后提交前进程崩溃（事务回滚、租约仍为 candidate、文件已不在）→ 下次裁决发现磁盘缺失时将租约收敛为 `deleted`，本次不增加 freed 计数；删除已提交而 HTTP 响应丢失、管理员重试 → 文件不在 `scan_store` 中且租约为终态，运行为空操作、返回 0，不重复计数。
- **FR-9（终态租约清理）**：`deleted` 终态租约保留一个可复核窗口后由后续 purge 运行清理；若终态租约对应 digest 在磁盘上重新出现（如备份恢复），允许按当前磁盘身份重新建立候选，不得使旧孤儿永久逃逸清理。
- **FR-10（统计与旁路不变）**：`collect`/`scan_store` 的所有口径（nominal/real/saved/orphan/missing/stored、共享 digest 计一次、暂存文件清理与计数、缺失文件报告）保持不变；租约表不参与这些统计；purge 返回值仍为 `(removed, freed)`，其语义仅包含 staging 清理数与本次确认 unlink 成功的文件。
- **FR-11（接口兼容）**：`POST /web/admin/storage/purge` 请求/响应形状与鉴权不变；`purge_orphans(session, root, *, grace)` 签名保持兼容。

## Non-Functional Requirements

- **NFR-1（安全性/正确性）**：在 PostgreSQL 上，清理裁决与上传注册/撤销之间必须由实际行锁串行化，不存在"引用已提交但文件仍被 unlink"的交错；SQLite 上靠写串行与事务边界保持逻辑等价。裁决采用"宁可保留、绝不误删"的失败方向。
- **NFR-2（活性）**：任何事务持有的锁都随提交/回滚/进程退出释放；不存在需要人工干预数据库才能解除的上传阻塞；候选不会被无限重试的 unlink 失败反复打扰（有退避），但故障解除后可在有限次运行内收敛。
- **NFR-3（诚实计量）**：只有本进程本次运行 unlink 成功且事务提交的字节计入 freed；外部消失、崩溃残留、未知状态一律计 0。
- **NFR-4（可测试性）**：竞态交错可用服务层确定性交错测试覆盖（SQLite 测试库）；锁语句的存在与顺序在代码中可核查；既有测试全部不改动即通过。
- **NFR-5（风格一致）**：遵循本模块既有惯例：详细模块/函数 docstring 说明"为什么"、`FOR UPDATE` 的 SQLite 注释惯例、无新增配置、同步文件操作放线程外直接调用（purge 由管理请求触发，沿用现状）。

## Constraints

- **Technical**: Python 3、SQLAlchemy 2.x async、FastAPI；同时支持 SQLite（aiosqlite，`FOR UPDATE` 被方言忽略）与 PostgreSQL；Alembic 迁移需 batch 模式兼容 SQLite；不得引入新依赖。
- **Business**: 清理仍需管理员密码二次确认；24h 宽限不变；删除不可逆，误删附件是最高等级事故。
- **Dependencies**: `storage_uploads`、`storage_downloads`、`items`/`item_fields` 表既有结构不变；迁移链接在 `d7cd57abc8a4` 之后。

## Assumptions

- 宽限期仍以文件 mtime 度量，租约 `available_at = mtime + grace`：租约提供的是跨请求的持久仲裁点与持锁复核，而非在文件年龄之外再增加一段等待——因此已超过 24h 的孤儿仍在一次 purge 调用内完成"获取租约→裁决→删除"，现有测试与运维体验不变。
- "在途下载所有权"以未过期 `storage_downloads` 行为准（5 分钟窗口覆盖 302→GET）；文件开始流式传输后的极端窗口沿用现状（内容寻址 + item 引用本身亦在复核集合内，双重保护）。
- 单实例部署；不处理两台服务器共享同一存储目录的情况。
- unlink 失败退避采用内部固定策略（按尝试次数指数退避、上限为 grace），不暴露为设置项。

## Acceptance Criteria

### AC-1: 旧孤儿在一次 purge 调用内经租约正常删除（行为保持）
- **Type**: `rule`
- **Given**: store 中存在 mtime 超过 24h、无任何库引用的 digest 文件
- **When**: 调用 `purge_orphans(session, root, grace=24h)`
- **Then**: 文件被 unlink，返回 `(1, 文件大小)`，租约表留下 `deleted` 终态记录
- **Pass Condition**: 既有 `TestPurgingOrphans::test_an_old_orphan_goes_when_it_is_asked_for` 不修改即通过，且新增租约状态断言通过
- **Evidence**: pytest tests/test_storage_stats.py 结果 + 租约表查询

### AC-2: 引用查询后即时挂载（exists=1）提交的新上传不会被删
- **Type**: `rule`
- **Given**: 旧孤儿 digest D 已取得 candidate 租约；一个附件发起上传且 D 文件在磁盘上
- **When**: `storage.authorize` 走即时挂载路径
- **Then**: 候选租约被撤销（删除），item 正常挂载 md5，purge 裁决因无租约/有引用而不删除 D，文件保持存在
- **Pass Condition**: 服务层交错测试证明 authorize 后磁盘文件存在、item 字段含 D、租约不再是 candidate；随后 purge 删除数为 0
- **Evidence**: 新增 pytest 测试

### AC-3: 清理先完成删除时，并发上传收敛为正常发送字节
- **Type**: `rule`
- **Given**: candidate 租约存在，裁决事务已 unlink 并提交（租约 deleted、文件缺失），此时客户端对同一 digest 调用 authorize
- **When**: authorize 持锁复查磁盘后发现文件缺失
- **Then**: 不挂载缺失文件，而是创建 StorageUpload 并返回上传指引；客户端发送字节并 register 后附件完整可用
- **Pass Condition**: 测试完整走完 authorize→bytes→register，item 挂载成功且文件在盘
- **Evidence**: 新增 pytest 测试

### AC-4: 在途 upload 行阻塞裁决；register 提交后引用阻塞裁决
- **Type**: `rule`
- **Given**: candidate 租约 + 同 digest 的未完成 StorageUpload 行（或刚 register 完的 item 引用）
- **When**: purge 裁决运行
- **Then**: 候选保留、文件不删；upload 完成注册后文件身份变化会使下次运行释放旧租约
- **Pass Condition**: 两种状态下 purge 均返回删除数 0 且文件存在
- **Evidence**: 新增 pytest 测试

### AC-5: 未过期下载授权保留候选
- **Type**: `rule`
- **Given**: candidate 租约 + 同 digest 一条未过期 StorageDownload
- **When**: purge 裁决
- **Then**: 候选保留、文件不删；下载授权过期后（且无其他所有者）才允许删除
- **Pass Condition**: 未过期→保留；人工置为过期→可删除
- **Evidence**: 新增 pytest 测试

### AC-6: 引用复核失败保留候选
- **Type**: `rule`
- **Given**: candidate 租约，裁决时发现 digest 被某个库 item（含其他库共享、回收站 item）引用
- **When**: purge 裁决
- **Then**: 租约保持 candidate 且快照字段不变，文件不删，返回不计费
- **Pass Condition**: 断言租约仍为 candidate、identity 字段未变、文件存在
- **Evidence**: 新增 pytest 测试

### AC-7: 文件身份变化释放租约
- **Type**: `rule`
- **Given**: candidate 租约记录了 size/mtime_ns 快照
- **When**: 文件被新落盘替换（store_file 的 os.replace 产生新 mtime/尺寸）后裁决
- **Then**: 租约被删除（释放）；本次不删文件；新文件因 mtime 年轻不会在同次成为候选
- **Pass Condition**: 租约行消失、文件存在、本次返回 (0,0)，且 24h 后可重新走候选流程
- **Evidence**: 新增 pytest 测试

### AC-8: unlink 失败记录为可重试清理
- **Type**: `rule`
- **Given**: candidate 租约，unlink 持续抛 OSError（monkeypatch）
- **When**: purge 裁决
- **Then**: 文件保留；租约 attempts 递增、last_error 记录、retry_after 被置为未来；返回 (0,0)；退避期内再次 purge 不重试；故障解除且过退避期后重试成功
- **Pass Condition**: 上述四点全部可断言
- **Evidence**: 新增 pytest 测试

### AC-9: 崩溃于 unlink 之后提交之前可收敛且不冒领空间
- **Type**: `rule`
- **Given**: candidate 租约 + 文件已被外部移除（模拟 unlink 后回滚）
- **When**: purge 裁决，及管理员随后再次触发 purge
- **Then**: 第一次将租约收敛为 deleted 且 freed 计 0；第二次为空操作返回 (0,0)；此后该 digest 的新上传不被阻塞
- **Pass Condition**: freed 两次均为 0；租约 deleted；新上传三步流程成功
- **Evidence**: 新增 pytest 测试

### AC-10: 进程退出不遗留永久阻塞
- **Type**: `rule`
- **Given**: 任意中断后重启（租约停留在 candidate；无活动事务）
- **When**: 客户端对相关 digest 发起全新上传
- **Then**: authorize 正常清理/忽略残留租约并完成授权，无需人工操作数据库
- **Pass Condition**: 残留 candidate 存在时上传全流程成功，租约最终被撤销或正确释放
- **Evidence**: 新增 pytest 测试

### AC-11: 统计与旁路行为完全不变
- **Type**: `rule`
- **Given**: 共享 digest、缺失文件、staging 残片、正常下载/查看场景
- **When**: 运行 collect/scan_store/purge 及附件下载
- **Then**: nominal/real/saved/orphan/missing/stored 数值、staging 清理计数、302 与文件响应行为全部与改动前一致
- **Pass Condition**: 既有 tests/test_storage_stats.py、tests/test_files.py、tests/test_web_admin.py 全部不修改通过
- **Evidence**: 既有测试套件全量通过

### AC-12: 数据库迁移与模型一致性
- **Type**: `rule`
- **Given**: 处于 head `d7cd57abc8a4` 的数据库
- **When**: 执行 alembic upgrade 再 downgrade
- **Then**: upgrade 创建租约表与索引且模型可用；downgrade 干净删除；`create_all` 建表与迁移结果兼容
- **Pass Condition**: 升级/降级命令成功，测试库 create_all 后所有测试通过
- **Evidence**: alembic 命令输出 + 全量测试

### AC-13: 仲裁锁定设计的严密性
- **Type**: `rubric`
- **Dimension**: 并发正确性与代码可核查性
- **Scale**: 1-5
- **Anchors**: 1 = 仍存在无窗口保护的 unlink 或新增永久阻塞点；3 = 主要竞态关闭但锁定顺序/崩溃收敛有一处含糊；5 = 每个交错都有明确的锁顺序、崩溃方向（偏向保留）与收敛路径，docstring 完整说明，评审可逐行核查
- **Pass Threshold**: >= 4
- **Evidence**: 独立评审对裁决事务、撤销路径、收敛矩阵的逐行核查

## Open Questions

- 无阻塞性问题。以下决策已按 Assumptions 处理，评审时可否决：
  - [ ] 是否接受"不在文件年龄之外增加第二段等待"（available_at = mtime + grace），保持单次调用可删除旧孤儿？
  - [ ] unlink 失败退避策略（指数退避、上限 grace、无配置项）是否认可？

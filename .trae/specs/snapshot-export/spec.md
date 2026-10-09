# 快照一致的文献库导出 - 产品需求文档

## Overview
- **Summary**: 重构管理员导出文献库归档（ZIP）的实现，使一次导出的全部内容——manifest 中的库版本、全部对象、对象间关系、删除记录和每个附件——绑定到同一个可确认的导出边界（库快照），校验通过后才把归档原子发布到最终名称；进程重启可回收未发布临时文件，且永远不删除已确认归档。
- **Purpose**: 当前实现分多次查询对象、附件直接写入最终 ZIP。并发编辑会让清单、关系、文件来自不同版本（静默混合内容）；磁盘写满会留下一个看似可下载的残缺归档。
- **Target Users**: 通过网页或命令行导出/导入归档的实例管理员：个人库的所有者、群组库的管理员。

## Goals
- 导出内容与某一个可确认的库快照严格一致，不产生跨版本混合内容。
- 附件（文件系统资源）与数据库快照属于同一导出边界；附件在导出期间消失或被替换时中止而非静默跳过。
- 归档先在临时工作区写入并完整校验，再原子发布到最终路径；任何失败路径都清理对应资源并给出可诊断结果。
- 进程重启后可识别并回收未发布的临时工作区；已确认归档在任何自动流程中都不被删除。
- 下载端在任何时刻都无法获取半成品。
- 保持既有行为兼容：空库导出、共享 digest 去重、既有归档格式版本、网页端权限检查、旧归档导入。

## Non-Goals
- 不改变同步协议、对象版本编号规则与归档内 JSON 文档（items.json 等）的现有字段语义。
- 不提供归档内容的加密、账号/凭据携带能力（现有设计刻意排除）。
- 不新增 Web UI 页面或管理界面（除必要的错误提示文案外）。
- 不对已发布归档做自动过期清理；不因本次改动引入定时清扫任务。
- 不改变导入侧除"兼容新增 manifest 字段"以外的恢复语义。

## Background & Context
- 现状代码：
  - [transfer.py](file:///Users/ding/Documents/swe/0910091/project-02/src/altero/services/transfer.py) 的 `export_library` 对同一 session 发出十余次独立 SELECT（items、collections、searches、tags、membership、item_tags、settings、fulltext、deletions），再按 item.fields 中的 md5 收集附件路径并直接打开 `destination` ZIP 写入；缺失的 backing 文件被静默跳过。
  - [webtransfer.py](file:///Users/ding/Documents/swe/0910091/project-02/src/altero/api/routes/webtransfer.py) 的 `download_archive` 在系统临时目录 `mkdtemp` 中构建 ZIP，用 `FileResponse` 流式发送，响应结束后删除目录。
  - [cli.py](file:///Users/ding/Documents/swe/0910091/project-02/src/altero/cli.py) 的 `library export` 直接写用户给定路径。
- 数据库：SQLite（WAL，读事务看到一致快照）与 PostgreSQL 均支持；写路径通过 `lock_library` 行锁 + `bump_library_version` 串行化，每次写产生一个新的库版本。
- 附件存储：按 md5 内容寻址，`storage.store_file` 以临时名 + `os.replace` 保证 digest 路径上整文件或不存在；digest 路径在约定上不可变。系统允许"item 声称的 digest 没有 backing 文件"这一常规状态（WebDAV 迁移等）。
- 分层约束：`services/` 内模块不得 import fastapi/starlette/uvicorn（test_architecture.py 强制）。

## Functional Requirements
- **FR-1 快照边界（数据库部分）**：一次导出的库行（含版本）、items/collections/searches/tags/settings/fulltext/deleted 全部对象及 CollectionItem、ItemTag、ItemRelation、CollectionRelation 等关系，必须来自同一个数据库事务快照。
- **FR-2 并发写入处理**：导出期间目标库发生写入并提交时，导出内容必须等于边界版本的稳定旧快照；若数据库无法继续维持该快照（如序列化/快照失效错误），导出必须明确中止并报错，允许调用方重新开始。禁止静默生成混合内容。
- **FR-3 快照边界（附件部分）**：边界时存在 backing 文件的 digest 集合属于导出内容。附件字节在读取入归档时消失，必须以"附件消失"类错误中止；附件在边界与最终校验之间被替换（大小或修改时间变化），必须以"附件已变更"类错误中止。边界时本就没有 backing 文件的 digest 维持现状（不纳入归档，非错误）。
- **FR-4 校验后发布**：归档必须写入最终目标之外的临时路径；关闭后重新打开完成完整性校验（ZIP CRC、成员集合、manifest counts 与实际文档/文件记录一致）后，才以原子重命名发布到最终路径。
- **FR-5 失败诊断与清理**：附件消失/被替换、临时归档写入失败、最终发布失败必须分别以不同的异常类型报告，错误信息含库标识（及 digest 等相关上下文），并记录日志；对应临时资源（工作区/半成品文件）被清理，最终路径保持不存在或原状。
- **FR-6 重启回收**：进程启动时在服务端导出目录中识别未发布的导出临时工作区；超过宽限期的工作区判定为死进程遗留并删除，宽限期内的保留。
- **FR-7 已确认归档保护**：任何自动回收流程只处理未发布临时工作区，永不删除最终路径上的归档文件，包括导出目录中已发布但尚未被下载清理流程删除的文件。
- **FR-8 下载端隔离**：Web 下载仅在归档成功发布后才返回文件响应；导出失败时返回错误响应，下载端无法访问半成品；临时工作区不被任何静态/下载路由暴露。
- **FR-9 兼容性行为**：空库可以导出并重新导入；多个附件共享同一 digest 时归档中只保存一份；归档 `FORMAT_VERSION` 不提升，manifest 仅新增可选字段；无新增字段的旧归档可正常导入；未来格式版本仍被拒绝；Web 导出权限检查规则不变。
- **FR-10 运维入口**：提供命令行回收命令，可对指定目录（默认服务端导出目录）执行未发布工作区回收并报告结果。

## Non-Functional Requirements
- **NFR-1 正确性**：导出事务在 PostgreSQL 上使用可提供稳定快照的隔离级别；在 SQLite（WAL）上同样读到一致快照。
- **NFR-2 磁盘占用**：附件以流式写入归档，不把整个库或整份附件长期驻留内存，也不额外保留一份完整附件副本（归档本身之外不产生等量级临时占用）。
- **NFR-3 可观测性**：中止、写入失败、发布失败、启动回收均有日志（含上下文），异常类型彼此可区分。
- **NFR-4 启动安全**：启动回收失败不阻断应用启动；回收遵守宽限期，不影响其他进程正在进行的导出。
- **NFR-5 分层合规**：核心服务代码不依赖 Web 框架。

## Constraints
- **Technical**: Python 3 + SQLAlchemy async（async session）；需同时兼容 aiosqlite 与 PostgreSQL；原子发布要求临时工作区与最终路径在同一文件系统（`os.replace` 不跨设备）。
- **Business**: 不破坏既有客户端与既有归档；不删除已确认归档。
- **Dependencies**: 需要新增一个服务端导出目录配置项（默认值即可工作）；启动恢复挂在应用 lifespan 中。

## Assumptions
- digest 路径上的文件在存储约定上不可变：正常写路径只在上传时以 `os.replace` 落入新 digest 路径。因此附件最终校验比较源文件的 `(size, mtime_ns)` 足以发现替换；归档内另存附件自身的 sha256/大小作为产物记录与导入侧校验依据。
- 网页下载为一次性流式响应，响应结束后删除其已发布文件是既有且被接受的行为；进程在响应期间重启导致该文件残留时，由运维人工处理，自动流程不删除它。
- CLI 导出的目标父目录可能是任意路径，启动时只回收服务端导出目录；CLI 回收命令可用于任意指定目录。

## Acceptance Criteria

### AC-1: 全部内容绑定同一导出快照
- **Type**: `rule`
- **Given**: 一个正在被同步（可能随时被写入）的文献库
- **When**: 管理员发起一次导出
- **Then**: manifest 中库版本、七份 JSON 文档中的全部对象及其关系、删除记录、归档附件清单来自同一个事务快照；导出过程中多次查询不跨版本
- **Pass Condition**: 导出在 REPEATABLE READ 级别的单一事务中完成全部数据库读取；测试证明导出进行中他处提交的写入不改变导出内容
- **Evidence**: 代码审查 + 并发场景测试（导出期间提交写入，归档内容仍等于边界版本）

### AC-2: 并发写入：稳定旧快照或明确中止
- **Type**: `rule`
- **Given**: 导出进行中，目标库发生一次成功写入提交
- **When**: 导出完成或数据库快照失效
- **Then**: 默认产出边界版本的稳定旧快照（内容不受新写入影响）；若数据库报告快照/序列化失效，则抛出导出冲突类错误中止，最终路径不产生文件，调用方可重新发起
- **Pass Condition**: 测试覆盖"并发提交不影响导出内容"；存在冲突错误路径且中止时无目标文件、无残留工作区
- **Evidence**: 并发测试 + 故障注入测试

### AC-3: 附件消失或被替换时中止
- **Type**: `rule`
- **Given**: 某 digest 在导出边界时存在 backing 文件
- **When**: 该文件在附件读取阶段消失，或在边界与最终校验之间被替换（size 或 mtime 变化）
- **Then**: 分别抛出"附件消失"或"附件已变更"错误，导出中止；临时工作区被清理；最终路径不存在
- **Pass Condition**: 两类故障注入测试通过，异常类型、message（含库与 digest）可区分
- **Evidence**: tests 故障注入

### AC-4: 校验通过后才发布到最终名称
- **Type**: `rule`
- **Given**: 导出进行到归档写入完成
- **When**: 归档通过/不通过完整性校验
- **Then**: 通过时以 `os.replace` 原子发布到最终路径；不通过时中止、清理，最终路径保持原状；最终路径上任何时刻都不存在半截 ZIP
- **Pass Condition**: 校验包含 ZIP CRC（testzip）、成员集合、manifest counts 与文档长度/附件记录一致；发布为同目录原子重命名；有测试证明失败时最终路径不变
- **Evidence**: 代码 + 测试

### AC-5: 临时归档写入失败的诊断与清理
- **Type**: `rule`
- **When**: 临时归档写入因磁盘满/IO 错误失败
- **Then**: 抛出写入失败类异常（含库标识与原因），记录日志，半成品与工作区被清理，最终路径不变
- **Pass Condition**: 故障注入（写/flush 阶段 OSError）下异常类型正确且目标父目录无 `.altero-export-*` 残留、最终路径不存在
- **Evidence**: 故障注入测试

### AC-6: 最终发布失败的诊断与清理
- **Type**: `rule`
- **When**: `os.replace` 发布失败（如设备/权限错误）
- **Then**: 抛出发布失败类异常（含源与目标信息），记录日志，工作区被清理，最终路径不变
- **Pass Condition**: 故障注入（os.replace 抛 OSError）下异常类型正确、无工作区残留、最终路径不存在
- **Evidence**: 故障注入测试

### AC-7: 重启回收未发布工作区
- **Type**: `rule`
- **Given**: 服务端导出目录中存在上次进程遗留的未发布导出工作区
- **When**: 进程启动执行回收
- **Then**: 超过宽限期的工作区被删除并计数记录日志；宽限期内的保留；回收失败不阻断启动
- **Pass Condition**: 测试覆盖旧工作区被删、新工作区保留、回收异常被捕获并记录
- **Evidence**: 恢复函数测试

### AC-8: 已确认归档永不被自动删除
- **Type**: `rule`
- **Given**: 导出目录或任意目标目录中同时存在已发布归档（.zip）与未发布工作区
- **When**: 执行启动回收或 CLI 回收
- **Then**: 已发布归档文件保持不变，仅未发布工作区可能被删除
- **Pass Condition**: 回收测试中最终 .zip 文件（含导出目录中的已发布文件）始终存在且内容不变
- **Evidence**: 恢复函数测试

### AC-9: 下载端永远看不到半成品
- **Type**: `rule`
- **Given**: 任意导出状态（进行中、失败、成功）
- **When**: 浏览器请求归档下载
- **Then**: 仅发布成功后返回归档响应；进行中/失败时外部无法通过任何 URL 访问到工作区文件；失败返回错误状态
- **Pass Condition**: Web 测试证明成功路径内容正确，失败路径返回错误 JSON 且导出目录无对外暴露的半成品
- **Evidence**: tests/test_web_transfer.py

### AC-10: 空库可导出并重新导入
- **Type**: `rule`
- **Given**: 一个没有任何对象的空库
- **When**: 导出并将所得归档导入空目标库
- **Then**: 导出成功，manifest counts 全为 0、归档不含 files 成员；导入成功且仍为空
- **Pass Condition**: 空库导出/导入往返测试通过
- **Evidence**: tests

### AC-11: 共享 digest 去重保持
- **Type**: `rule`
- **Given**: 两个或多个附件引用同一 md5
- **When**: 导出
- **Then**: 归档中 `files/<digest>` 仅一份；manifest 附件记录中该 digest 仅一条；counts.files 按 digest 计数
- **Pass Condition**: 去重测试通过
- **Evidence**: tests

### AC-12: 格式版本与旧归档兼容
- **Type**: `rule`
- **Given**: FORMAT_VERSION=1 的现有归档（无新增 manifest 字段）与本次实现产出的归档
- **When**: 分别导入，并尝试导入未来格式归档
- **Then**: 旧归档正常导入；新归档 format 仍为 1 且可被旧逻辑读取（新增字段均可选）；未来格式仍被拒绝
- **Pass Condition**: 用去除新增字段的归档完成导入测试；format 断言保持 1；未来格式拒绝测试保持通过
- **Evidence**: tests

### AC-13: 网页导出权限检查不变
- **Type**: `rule`
- **Given**: 所有者、群组管理员、普通成员、未认证者
- **When**: 请求归档下载
- **Then**: 权限判定与现状完全一致（个人库所有者；群组管理员；其他拒绝）
- **Pass Condition**: 现有 test_web_transfer.py 权限测试全部通过，无需修改其断言语义
- **Evidence**: 现有测试

### AC-14: CLI 回收命令
- **Type**: `rule`
- **Given**: 含未发布工作区的目录
- **When**: 执行 CLI 回收命令（指定目录，默认服务端导出目录）
- **Then**: 超过宽限期的工作区被回收，命令报告回收数量；已发布归档不受影响
- **Pass Condition**: 命令处理函数测试通过；默认参数指向服务端导出目录
- **Evidence**: tests

### AC-15: 失败结果的可诊断性
- **Type**: `rubric`
- **Dimension**: 各类失败（冲突中止、附件消失、附件变更、写入失败、发布失败）的区分度与上下文完备性
- **Scale**: 1-5
- **Anchors**: 1 = 全部混为同一错误或信息无法定位问题；3 = 异常类型可区分但缺少 digest/路径等上下文；5 = 异常类型彼此独立，message 与日志含库标识、digest/路径、阶段，运维可直接定位
- **Pass Threshold**: >= 4
- **Evidence**: 代码审查 + 全部故障注入测试的断言

### AC-16: 资源清理完整性
- **Type**: `rubric`
- **Dimension**: 各成功/失败路径后文件系统残留情况
- **Scale**: 1-5
- **Anchors**: 1 = 失败后工作区与半成品长期残留；3 = 主路径清理但个别故障路径残留；5 = 成功路径仅留最终归档，全部失败路径无工作区/半成品残留，重启后旧残留也被回收
- **Pass Threshold**: >= 4
- **Evidence**: 每条故障路径后对目标父目录与导出目录的文件列举断言

## Open Questions
- 无（需求中的分支选择已按"稳定旧快照为主、快照失效则中止；附件边界严格校验"确定）。

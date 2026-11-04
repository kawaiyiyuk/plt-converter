# 程序版纸样识别与排版服务候选

当前是独立私有服务的代码候选；V6b 是几何实验，不是可靠裁片识别结论。无模型调用、无人工补识别。没有部署到生产，也未验证真实绘图设备、实际投影仪、Docker 运行或手机完整流程。

## 边界与证据保留

`app/services/pattern_layout/` 移植冻结 V6b 的闭环、源路径独立性、闭面关系及审计内核，改为包内相对导入；删除研究运行器和帧跟踪依赖。正式 `plt_parser` 公开每段的 LT、PW 和 LT 代数；旧 strict 仍拒绝 LT，旧 projection 仍保留 LT 不支持警告，原 PDF 渲染语义保持。

端点严格闭合但自交的原path也保留完整源段，不能因 Shapely is_ring=false 丢弃；make_valid 只用于域内线选取，outer/源段不替换，区域歧义明确标注。每个完整原闭环另保留 `S-shape-*` 几何候选，内容包含域内全部精确片段，不会因为被猜成大框、嵌套或套码而消失。该候选可能同时包含别的真实裁片；`semantic_piece_verified=false` 和歧义提示必须展示，不能将几何包含宣称为真实独占归属。原 V6b 分组、辅助和完整审计保留在独立 audit 资源，可原样取回；不把仅有支持轮廓的辅助冒称完整候选。

另外按完整独立源闭链证书，逐个矩形提供“去该组矩形框线”备选；只减形成矩形边界的原源段，保留框内/周边线，原候选不改。明确显示“可能移除本片真实标记”，不按固定尺寸或样本ID判定标尺，不删除真实小矩形原候选，也不对主体自身的矩形外线生成去框备选。

片段永久使用 `path_id/segment_index/t0/t1` 引用，不扩大到完整 path。原件必须由业务后端长期私有保存；本服务 queue 仅为临时任务存储。

## 私有 HTTP 合同

所有接口都必须带 `Authorization: Bearer <PATTERN_LAYOUT_SERVICE_KEY>`。任务、资源和下载额外带 `X-Pattern-Layout-Owner: <业务后端已验证user_id>`；不匹配返回404。该 header 只能由受信业务后端生成，不直接开放给浏览器。无任意 URL 抓取。

- `GET /internal/pattern-layout/capabilities`：schema、实际 runtime_version、实验状态。
- `POST /internal/pattern-layout/jobs`：multipart `file`，异步识别，202 `{id,status,retention_seconds}`。
- `POST /internal/pattern-layout/client-results`：multipart `file` + gzip `result`，核验浏览器识别结果后走同一归档流程；契约与边界见下节。
- `POST /internal/pattern-layout/export`：multipart `file` + `project` 第二文件part（`project.json`, `application/json`, UTF8 JSON），异步导出，同样202。
- `GET /internal/pattern-layout/jobs/<id>`：JSON 状态 `queued/processing/cancelling/done/failed/cancelled`；识别完成含轻量 `result`；导出完成含 `result_ready:true`。
- `GET /internal/pattern-layout/jobs/<id>/result`：仅成功导出的 PLT bytes。
- `DELETE /internal/pattern-layout/jobs/<id>`：在途任务请求取消（监督进程 TERM→KILL，并在 join 确认回收后释放 CPU 槽位）；已完成任务释放临时资源。
- `GET /internal/pattern-layout/jobs/<id>/source-geometry`：原图 paths 和共享 style_table 的 gzip JSON。
- `GET /internal/pattern-layout/jobs/<id>/candidates/<candidate_id>`：单候选精确 outer/fragments 的 gzip JSON。
- `GET /internal/pattern-layout/jobs/<id>/audit`：全部实验数据及完整审计的 gzip JSON，不能自动在手机展开。
- `GET /internal/pattern-layout/jobs/<id>/archive`：流式 ZIP_STORED 包，包含 index.json、所有 gzip 原图/候选/audit 和 manifest.json；manifest 提供每个 member 的 SHA256/bytes/source/runtime。后端须将这些 member 原样迁移至本人私有存储并验证后，才向用户标识别 done，不依赖引擎 TTL。
- `POST /internal/pattern-layout/jobs/<id>/ack`：后端仅在完整归档 durable 后提交 `{durable_complete:true,archive_sha256:...}`。用户确认未归档临时识别结果最长保留24小时；完成后计时，处理中不按该完成TTL清理。过期可用长期原件重生成，不删除原件、已保存工程或本机草稿。ACK后及时回收重资源，只留轻量幂等元数据/索引至完成TTL；未ACK默认DELETE409，但用户明确删源等场景可用同owner的 `DELETE ?discard=true` 显式丢弃，不伪造durable ACK。失败未ACK可在24小时内重试；仍占明确磁盘预算，容量不足公开503。

`X-Idempotency-Key` 可用于同用户任务重试；同 key 不同内容409。同 key 相同内容返回已有任务。未传时按 owner、kind、源字节和规范 project 自动复用在途/完成结果。

轻量索引保留全部 pieces/auxiliary id、bounds、状态、issue_codes、fragment_count、resource_id；不随主响应带 fragments/详细审计。`preview_outline` 明确为0.5mm容差的缩略预览（simplified=true/not_for_export=true），精确 outer 仍在单候选资源中。原图路径使用 `segment_style_ids` 引用 `style_table`。单候选每段增加 `preview_phase_mm`，与导出共用原 path 的 LT 代数累计相位，不修改原区间。

指纹已分责：recognition runtime_version 覆盖内核、正式parser及number_tokens、源适配、精确函数源码、分层资源和矩形后处理；export_runtime_version覆盖导出、共享相位/数值函数、源parser和独立验证器。两者均覆盖Shapely/GEOS/CAPI、Python、NumPy、架构和系统。导出独占代码改变不会使识别缓存失效，且测试证明；不同实际运行时不复用旧识别缓存。

## 浏览器识别结果核验与归档

`POST /internal/pattern-layout/client-results` 接收 multipart `file`（原件PLT）与 `result`（gzip 后的浏览器识别结果 JSON），异步任务 kind 仍为 `recognize`，返回、轮询、候选/原图/审计/归档资源、ACK、DELETE 与普通识别任务一致。鉴权与 `X-Pattern-Layout-Owner` 要求同 `POST /internal/pattern-layout/jobs`；`X-Idempotency-Key` 语义不变，未显式提供时 content_key 同时包含原件字节和结果字节，避免同一原件不同识别结果互相复用。

`result` gzip 原始字节上限 8 MiB，解压上限 64 MiB（流式限读；超限 HTTP 413 `client_result_too_large`）。payload 合同 `format=pattern-layout-client-result`、`format_version=1`：`source` 提供原件 sha256/name/path_count/text_count/warnings；`engine` 提供 engine_version、code_version、runtime_version 与 Pyodide/Python/NumPy/Shapely/GEOS runtime；`pieces`/`auxiliary` 的候选只携带 id、status、fragments（`path_id/segment_index/t0/t1`）、outer、可选 issues/parent/variant/loop/source_closed_path_ids/area/excluded_rectangle/content_complete_for_region（布尔）；服务端对浏览器候选一律写 `semantic_piece_verified=false`；`unassigned` 为 count/length_mm。候选总数≤5000、fragments 总数≤500000、单候选 outer≤50000 点，所有数字必须有限。

服务端只做结构核验，不重跑几何识别：核对原件 SHA256、`engine.code_version` 与服务端 `recognition_code_version()`、`engine.engine_version` 与服务端 `ENGINE_VERSION`；用服务端 `source_paths` 复核 path/text 计数与 warnings（逐字逐项）；每个 fragment 必须引用存在的源路径、整数且在范围内的 segment 及 `0<=t0<t1<=1`，a/b/style 由服务端按源重算；outer 必须落在原件毫米范围外扩 1mm 内，bounds 与文字 text_ids 由服务端重算。失败码为 `client_result_invalid`、`source_sha_mismatch`、`engine_code_mismatch`、`parser_mismatch`、`invalid_source_fragment`、`outer_out_of_bounds`，经现有 public_task_error 暴露，不透出内部异常。

核验通过后使用与服务端识别相同的分层资源/archive 产出，`result.source` 标记 `recognition_origin=browser`、`server_verification=structure_only`，并记录客户端 runtime/code_version；audit 资源保留客户端原始 payload（解压后的 JSON）。`GET /internal/pattern-layout/capabilities` 同时提供 `recognition_code_version` 与 `client_result_format_version:1`。该接口只证明提交结果与原件及同批代码在结构上一致，不证明候选由服务端算法产出，也不验证候选裁片语义、问题分级或外轮廓真实性；这些仍需按既有语义边界展示和人工核验。

## 坐标、导出和未支持边界

识别坐标是 mm，左上原点、x右/y下。工程 placements 使用 `{piece_id,anchor:{x,y},x,y,rotation_deg,mirror_x}`，先以 anchor 为原点镜像，再顺时针旋转，最后平移。导出 native HPGL 为 `x=layout_x*units_per_mm,y=-layout_y*units_per_mm`。

导出只使用精确源片段；不补线。保留原断笔、笔号/PW；数字用有限、可往返的普通十进制，不输出科学计数法。另一个独立线性解析器重读输出，核对段数、坐标、笔号、LT/PW 与可支持的 LB 文字位置/内容。

明确 LT2 absolute-mm 50/50 子集按原段累计长度保留相位，支持中段和镜像；每 path 预先线性计算相位，避免逐段倒扫。其他设备相关 LT 若截取中段无法保证相位则明确失败。原生 LB 支持平移；旋转/镜像 LB 字形、设备字体参数仍为阻断，不静默丢文字，也不将其写成已经通过的首版能力。11份当前真实样本的文字均为矢量笔划，没有 LB，不能据此推导通用文字支持。

未知命令警告必须展示。只有用户明确选择继续后，project 才能携带 `acknowledged_source_warnings` 的完整原警告字符串；未确认则不导出。已丢失字形等可知损坏仍失败，不用确认掩盖。

既有 PLT 投影仍裁到墨迹范围，不能把 tile 左上当布料原点。新增仅 PLT 提供的 `source_bounds_mm={x,y,w,h}` 表示裁边相对工程原点的偏移；布料幅宽/长度仍来自工程。PDF和历史响应不增加该字段。网页专用 `X-Layout-Token` 只在没有 Bearer 时交给业务后端专用 identity 验证；Bearer 失败不降级。每次验证支持撤销；跨用户404，新投影继续沿用同用户单活动文档语义。

## 部署与策略配置

`Dockerfile.pattern-layout` / `compose.pattern-layout.yaml` 是独立私有网络服务示例，未改旧转换或投影 compose。无 `ports` 发布；仅已认证业务后端加入同一 private network。HTTP 一个 Gunicorn worker，单监督器 FIFO，每任务一个独立 subprocess，compose CPU 配额1核，Linux 子进程再绑定一个可用 CPU。数值库线程数1。Mac本地测试不能替代Linux cgroup验证。

容量、运行预算和绑定信息必须显式配置，不提供未经确认的正式默认值。临时完成结果保留24h为用户已确认规则，compose固定86400；本地测试可显式注入更短TTL，不可当产品值：

- `PATTERN_LAYOUT_SERVICE_KEY`、`PATTERN_LAYOUT_BIND_HOST`、`PATTERN_LAYOUT_BIND_PORT`
- `PATTERN_LAYOUT_ROOT`、`PATTERN_LAYOUT_DISK_BUDGET_BYTES`、`PATTERN_LAYOUT_MAX_RESULT_BYTES`
- `PATTERN_LAYOUT_JOB_RETENTION_SECONDS`、`PATTERN_LAYOUT_MAX_REQUEST_BYTES`、`PATTERN_LAYOUT_TASK_WALL_SECONDS`、`PATTERN_LAYOUT_TASK_MEMORY_BYTES`
- compose另需 `PATTERN_LAYOUT_STATE_DIRECTORY`、`PATTERN_LAYOUT_CONTAINER_ROOT`、`PATTERN_LAYOUT_MEMORY_LIMIT`、`PATTERN_LAYOUT_NETWORK_NAME`

缺失启动策略时清楚报错；secret未配置的应用拒绝请求。请求体过大413（project正式通道为文件part，不受普通form字段500KB默认内存上限）、临时存储不足503、结果预算不足任务 failed、TTL后404都影响用户，属于必须公开且待配置确认的 AI 工程保护，不是已经确定的产品数量/人数规则。单worker是真正FIFO排队，第二任务不因前一任务运行而拒绝。源文件/结果/状态落受控磁盘；内存队列只传路径。在途按最大结果预算预留，终态原子降至实际留存文件+控制余量，ACK后再降，不因为下载而删除24h幂等结果。已ACK无archive的completed识别不能按内容复用于新附件，显式相同operation幂等仍保留任务身份。重启恢复尚未执行的 pending；运行中任务若监督器重启则明确失败，Linux按PID启动身份核对清理并用父进程死亡信号避免孤儿。取消/失败状态可查询，完成任务按明确TTL自动回收。CPU磁盘锁覆盖父进程重启与遗留worker的短暂交叠，避免双计算。墙钟超限强制终止并回收。Linux子进程 RLIMIT_AS/CPU 施加显式预算；Mac 本地不声称内存硬限制生效，capabilities 明示 unverified_non_linux。任务限制与总体容器内存配额仍待Linux非生产压测确定，缺失保持不可用。

## 本地验证

按本仓 Python 依赖环境运行：

```sh
PYTHONPATH=tests:. python -m unittest test_pattern_layout_v1 test_pattern_layout_service test_pattern_layout_projection_origin
python scripts/verify_pattern_layout_samples.py --manifest /path/to/local-manifest.json --output /tmp/pattern-layout-fixtures
```

真实源路径仅在本机临时清单，原件不复制进仓库。F09原完整响应约87.36MiB；最终无删线分层（含矩形备选）主索引约90KiB、原图gzip约116KiB、最大单候选gzip约42KiB、全审计gzip约8.6MiB；含流式archive副本总留存约20.31MiB。直接识别→分层 worker 测量151.76秒、峰值约740MiB（macOS），不是生产容器或手机性能承诺。

## 本轮独立本机验收记录

父 Agent 用独立 SQLite 与本机模拟私有存储，通过真实 HTTP 验证了 F07：复用已完整归档资源，选择 `S-shape-1-omitrect-104`，141条原外线完整，4条独立矩形框线未包含；异步保存 revision 1、重开、原件 SHA 核对通过，导出62,729字节，临时快照字节一致并完成 ACK。该记录不是实际 COS、生产、真机或投影仪验收，也不证明裁片所有内容的语义归属。原完整候选仍保留，独立矩形可能是本片真实标记，必须展示备选标签并允许原图对照。

gzip时间戳恢复缺陷已修复：gzip固定mtime=0且不写filename，ZIP成员固定时间与权限；同内容跨目录、跨mock时钟的所有资源/manifest/整包字节SHA一致。运行时还纳入实际zlib版本。旧gzip仍可读取，旧对象及其原SHA不重写；新资源使用新的识别指纹，不能把旧压缩头差异误当几何差异。

确定性打包修复后的新鲜回归86项通过，包含旧投影/身份、实际PDF/tile坐标、目标反例与队列/资源边界。11份冻结几何核心之后的后处理/最终导出以逐字节核心一致证明和逐源重放分开记录，不能把不同完整运行时版本伪称同一冻结。归档包和重放证据只放本机临时输出，不将用户原始文件、私人路径清单或大样本拷入仓库。

最终资源验收使用 v4：11源与冻结core逐字节一致，完整v3内容除runtime元数据外相同；388,573条候选/辅助片段全值不变，跨目录/跨mock时钟的所有成员及整包SHA一致，22次最终导出均通过。v3仅保留为历史对照，不冒充最终运行时产物。

## 圆弧折线化透明提示（最新冻结v5）

仅裁片识别/导出适配器启用 `report_curve_approximation=True`，正式parser默认False，既有转换及PDF/PLT/HPGL投影调用不变。实际绘制的AA/AR圆弧、CI圆被线段化时，source.warnings提示：“圆弧/圆导出使用折线近似，圆弧分段理论弦高不超过0.05mm；不代表投影总误差，需实测。”抬笔AA/AR、SP0、零角度或未生成有效圆的命令不增加该提示。纯直线无此风险提示。用户未确认时沿用 source_warning_confirmation_required；确认完整原提示后才可导出，不删除草稿或旧工程。

90项新鲜回归通过。对实际冻结旧parser以AA/AR/CI比较，formal默认、projection默认、projection 0.049mm及strict 0.049mm四模式输出document逐值完全相同。仅新增warning opt-in语句/参数及适配器启用；移除这些新增钩子后parser/adapter AST与原冻结完全一致。11源命令流核验AA/AR/CI均为0，最新路径/样式/原warning与v4完全一致；388,573条片段不变，完整v4内容仅runtime元数据更新，22次最终导出与v4字节相同，确定性打包复验通过。v5为本次最新证据，v4保留历史对照。

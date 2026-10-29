# PLT Converter

独立的 PLT 转换服务，供缝纫记忆小程序调用。

## 当前能力

项目已包含：

- 健康检查
- PLT 临时上传限制
- 基础 PLT 元数据预览
- PDF 页面预览和页数识别
- PDF 矢量路径转 HPGL/PLT，图片 PDF 提供降级描边转换
- Python HPGL/PLT 矢量解析
- A4/A3/A2/A1/A0/Letter 分页 PDF 生成
- PDF 一键转换为 A0～A4 目标纸张或 1:1 整张单页，内部自动完成可靠拼版识别、PDF→PLT→PDF，不向客户端暴露中间文件
- 页边距、页码、单页输出和指定页选择
- Docker/Gunicorn 启动配置
- Redis + RQ 异步任务队列，带队列容量、用户并发、限流、去重和取消
- PLT/PDF 输入复杂度限制、超时、暂时性错误单次重试和临时文件清理

转换引擎位于 `app/services/plt_parser.py` 和 `app/services/pdf_renderer.py`，不会和缝纫记忆主业务耦合。

## 本地运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
flask --app run:app run --debug --port 8090
```

健康检查：

```bash
curl http://127.0.0.1:8090/health
```

预览元数据（同步轻量接口，受独立限流保护）：

```bash
curl -F "file=@sample.plt" http://127.0.0.1:8090/api/v1/plt/preview
```

异步任务提交：

```bash
curl -F "file=@sample.pdf" http://127.0.0.1:8090/api/v1/pdf/preview
curl -F "file=@sample.pdf" \
  -F "rows=5" -F "columns=4" -F "order=row" \
  -F "margin_mm=0" -F "output_rotation=90" \
  -F 'page_slots=[0,1,null,3]' \
  http://127.0.0.1:8090/api/v1/pdf/jobs
```

提交接口返回 `job_id` 和 `status`。任务状态包括 `queued`、`processing`、`done`、`failed`、`cancelled`。查询、取消、下载和预览图请求必须使用提交时相同的 `X-Client-Key`。

```text
POST /api/v1/plt/jobs
GET  /api/v1/plt/jobs/<job_id>
DELETE /api/v1/plt/jobs/<job_id>

POST /api/v1/pdf/preview
GET  /api/v1/pdf/preview/jobs/<job_id>
POST /api/v1/pdf/jobs
GET  /api/v1/pdf/jobs/<job_id>
DELETE /api/v1/pdf/jobs/<job_id>
POST /api/v1/pdf/repage/jobs
POST /api/v1/pdf/repage/inspect
GET  /api/v1/pdf/repage/jobs/<job_id>
DELETE /api/v1/pdf/repage/jobs/<job_id>
GET  /api/v1/pdf/repage/files/<job_id>.pdf
GET  /api/v1/pdf/previews/<preview_id>/<page>.png
```

完成任务后返回 `pdf_path`、`plt_path` 或 `preview_id/pages`。Redis 不可用返回 503；队列、用户容量或限流拒绝返回 429 和 `Retry-After`。`page_slots` 中的 `null` 表示保留空白格；PDF→PLT 的 `output_rotation` 支持 `0/90/180/270`，在裁边和拼版完成后整体顺时针旋转，不影响 PLT→PDF。图片 PDF 的降级描边精度不等同于矢量 PDF。

PDF→PLT 生成结果同样受 `PLT_MAX_PATHS`、`PLT_MAX_COMMANDS`、`PLT_MAX_POINTS` 和 `PLT_MAX_DIMENSION_MM` 约束；超限时任务失败，不返回本服务随后无法重新解析的 PLT。

`GET /api/v1/pdf/preview/jobs/<job_id>/layout-suggestion` 不在 API 请求线程内直接扫描 PDF。首次调用把分析放入 `pdf-layout-analysis` 队列并返回 HTTP 202 / `analyzing`，客户端继续轮询；完成后同一接口返回缓存建议。正式转换和排版分析分别由独立 RQ Worker 消费，两个队列互不等待；健康检查要求两个队列都存在活跃消费者。

`POST /api/v1/pdf/repage/inspect` 在用户选择 PDF 后立即返回原 PDF 页数和可复用的 `source_id`；临时源文件默认保留 1800 秒。`POST /api/v1/pdf/repage/jobs` 通过同一客户端的 `source_id` 复用文件，不再重复上传，`paper_size` 接受 A0～A4 或 `SINGLE`。`SINGLE` 把完整纸样按 1:1 输出为一页，四边加 10mm 空白；当前 PDF 1.4 结果页单边最多 5080mm（含空白），超出时任务失败并释放额度。成功结果同时返回原 PDF 页数、输出 PDF 页数、`output_width_mm` 和 `output_height_mm`，整张结果文件名使用 `-整张.pdf`。单页源 PDF 按 1×1 处理；多页源 PDF 仅在本服务排版元数据有效，或矢量接缝分析达到中/高可信度时自动转换。证据不足、纯图片多页或版式歧义会明确失败，不猜测拼版。三类转换共用每日 3 次免费额度，仅在成功生成文件后计次；处理中任务暂占名额，失败或完成前取消会释放。免费成功次数未满 3 次但名额均被处理中任务占用时，新请求先等待结果，不提前要求看广告。超额转换需完整观看激励广告，不扣布豆；永久 VIP 免广告且不限次数。广告支持失败或完成前取消后再试 1 次，计费服务暂时无法释放资格时，失败或取消任务会在后续查询中重试。预留有效期默认 150 分钟，覆盖 20 个队列任务、210 秒单次超时及一次重试的最坏生命周期。

生成文件后，任务先在内部 `finalizing` 状态确认计次；对外仍显示处理中。若计次服务短暂失联或任务状态写入失败，后续任务查询和转换 Worker 每 5 分钟的清理循环会重试确认，不重新生成文件。确认前会检查结果文件仍存在；若文件丢失，只允许服务间接口携带 `rollback_completed=true` 补偿释放该任务的已确认免费额度。只有状态正式成为 `done`，下载接口才提供结果文件。

## 纸样投影（PDF、PLT/HPGL，独立验证版）

```text
POST   /api/v1/pdf/projection/documents          multipart file
GET    /api/v1/pdf/projection/documents/<document_id>
GET    /api/v1/pdf/projection/documents/<document_id>/tile
POST   /api/v1/pdf/projection/documents/<document_id>/renew
DELETE /api/v1/pdf/projection/documents/<document_id>
POST   /api/v1/pdf/projection/documents/<document_id>/layout-analysis
GET    /api/v1/pdf/projection/documents/<document_id>/layout-analysis
POST   /api/v1/pdf/projection/documents/<document_id>/layout
```

每个接口通过主后台 `conversion/identity` 校验 `Authorization: Bearer ...`，会话仅创建者可用；不调用额度授权、计次、广告、布豆扣除或正式转换队列，不新增 VIP 门槛。限流与账本记录都在投影自有 SQLite，不经 Redis：上传按 owner 与 IP 两档计数，瓦片走 token bucket；取值全部来自显式 `PROJECTOR_POLICY_FILE`（代码不再提供 10/30 之类的默认值）。rate 行有界，写满时新请求 429 而不是无限增长。超限返回 429 与 `Retry-After`，存储或账本暂时不可用返回 503。原页、拼版瓦片、续期、手动应用和关闭都不访问 Redis；只有自动分析使用独立的 `pdf-projection-layout` 队列（Redis 仅承载该 RQ 队列），Redis 故障不阻止 renew/delete。

投影只使用用户自己的 Bearer，不配置或持有 `CONVERSION_SERVICE_TOKEN`／`PROJECTOR_SERVICE_TOKEN`。API 和专用 Worker 启动时发现 `CONVERSION_SERVICE_TOKEN` 键（即使为空）立即拒绝；独立本地准备 base 与 apply 候选镜像也只查询镜像 ENV 变量名，存在该键就拒绝，不打印值。Dockerfile 的空值覆盖无法删除继承 ENV 或镜像历史；必须使用经核验、不含旧转换服务密钥的来源镜像，不能以覆盖空串代替清理，旧转换镜像与密钥不动。

用户已确认：每个投影 API 进程最多同时进行 2 个不同 Bearer 的身份校验，相同 Bearer 的并发请求按 SHA256 摘要合并，只占 1 个槽；完成立即清除，不缓存成功或失败结果，下一个请求仍实时验证。超限立即返回 `503 projection_identity_busy`、`Retry-After: 1`，提示 1 秒后重试，不排队占用身份槽。上传、取图、续期和删除等全部身份接口适用，因此 renew／delete 也可能短暂繁忙；繁忙不删除文档、不续期，现有到期时间保持。此上限按进程计算，多进程总上限相应累加；不新增 IP 桶、冷却或 TTL，也不承诺阻止所有顺序请求洪泛。

身份后端明确 401／403 保留认证拒绝；其他 HTTP 错误、连接／超时、坏 JSON 或无有效身份均返回 `503 projection_identity_unavailable` 与 `Retry-After: 1`，不删除源文档、不续期也不主动使会话失效。只有投影自身返回的 `projection_not_found` 404 表示文档不存在、过期或不属于当前账户。

上传文件本体沿用 `PLT_MAX_UPLOAD_BYTES`（默认 20MB），接受 `.pdf`、`.plt`、`.hpgl`，优先使用 multipart 的 `original_filename` 确定格式，`source_format` 必须与文件类型一致。PDF 路径接受 1–200 页可读、未加密的文档，不截断页面；损坏、零页、超过 200 页及任意加密（含 owner-only）明确拒绝。沿用 `PDF_MAX_TOTAL_PIXELS`（默认 200,000,000，按页面 point 尺寸面积估算，不是嵌入扫描图片的原始像素数）及 `PDF_MAX_DRAWINGS`（默认 250,000）的解析保护。上传返回 HTTP 201：

```json
{"document_id":"64位随机hex","filename":"pattern.pdf","page_count":2,"pages":[{"index":0,"width_mm":70.55555555555554,"height_mm":105.83333333333333},{"index":1,"width_mm":52.916666666666664,"height_mm":35.27777777777777}],"width_mm":70.55555555555554,"height_mm":105.83333333333333,"expires_in":3600,"requires_metadata_choice":false}
```

PLT/HPGL 由独立投影子进程按宽松兼容方式解析为临时内部单页 PDF；成功后删除服务端上传副本，内部文档随会话清理，不生成下载结果或调用正式转换计次。上传与 summary 返回 `source_format=plt` 及 `rendering_warnings`，客户端跳过 PDF 自动分析／手动拼版；PLT 请求 `layout-analysis` 返回 409 `projection_plt_layout_not_needed`。派生文档单份64MiB／账户128MiB预算必须在 policy 显式配置，缺失时只对 PLT 返回503 `projection_plt_unconfigured`，PDF 路径仍可使用旧policy。默认1016 units/inch；统一0.265mm黑线，LB按固定8pt显示并警告；实际绘制IN/SP/PA/PR/PU/PD/CI/AA/AR/PW/LB，VS忽略并警告；未知命令跳过、无法解析或非有限参数忽略、未配对坐标尾参丢弃，并通过有界rendering_warnings提示兼容处理。成功仅代表已生成可绘制内容，不代表支持SC/IP/RO/BZ等命令语义；跳过它们可能导致图形缺失或尺寸错误，仍须核对纸样及实体尺校准。SP0/无参SP收笔、LB游标/控制符与固定8pt、0.049mm曲线精度保持；NUL空字符、无有效图形、累计非有限几何和原有命令/点/形状/文本/派生/超时预算仍明确拒绝。曲线近似、实际渲染点和其他资源保护详见产品知识库的[纸样投影](https://github.com/kawaiyiyuk/fengrenjiyi-product-docs/blob/codex/projector-mirror-plt/products/fengrenjiyi/features/pattern-projector.md)。这些投影规则不扩大正式转换的指令或样式能力。

`pages` 索引从 0 开始连续排列；宽高包含 CropBox、旋转和 UserUnit，保留浮点精度，顶层宽高为首页兼容字段。默认原页 `tile?view=page&page_index=0`，旧无参请求仍显示首页。页码非整数／越界或 view 非 page/layout 返回 400；`view=layout` 未应用拼版返回 409。六个瓦片几何参数必填：x_mm/y_mm 可以为负，width_mm/height_mm 正有限，width_px/height_px 为 1–2048 整数且面积不超过 4,000,000。统一 width_px/width_mm 比例要求另一轴取整误差最多 1 像素，不拉伸；超出页／拼版范围补白。全部响应 `Cache-Control: no-store`。

用户于 2026-10-13 选择“上传后自动一键排版，直接整张”；上传本身只保存原文档，客户端随后提交独立分析，可靠结果就绪后带精确 analysis_id 应用拼版。存在排版元数据时必须先明确选择“原排版”或“当前 PDF 内容”；缺选择返回 422。分析失败、超时、低置信或不支持自动推断时保留原分页，并可手动输入行列、按行／按列及四边裁边，不静默猜测拼版。

`POST layout-analysis` 表单 `metadata_mode=original/current`；无有效元数据时默认 current。有元数据并不证明 PDF 未被编辑，原排版选项是用户明确接受的旧信息。批准旧 base 中的转换算法仍只支持 `optimize_pdf_layout(source)`，保持原模块与签名；新投影通过 `projection_pdf_runtime` 加载独立私有模块，使用当前仓库同源的选页与分析能力，不要求更新旧转换。开发 checkout 从 `app/services` 以私有模块名加载；增量镜像将 `pdf_layout_optimizer.py`、`pdf_to_plt.py`、`pdf_metadata.py` 三份同源文件 COPY 到 `app/_projection_pdf/`。镜像私有运行时缺失或不完整时明确失败，不回退旧 base 的转换模块。投影适配层按 current 仅在分析副本移除 Subject，原字节不动，original 可复用完整原排版元数据。矢量分析以跨页接缝、近似一致纸张和 medium/high 置信度为准；纯扫描、混纸张尺寸、少量／含糊接缝可能无法自动判断，边缘红线仍按旧算法作为裁剪辅助线忽略，仅影响分析证据，原色渲染仍保留。

POST 返回 HTTP 202 `{analysis_id,status:"analyzing",phase:"queued"}`；同模式进行中幂等复用，先检查失败／失联状态避免永久分析中。GET 返回当前 analysis_id 及 status（analyzing／ready／insufficient_evidence／failed），进行中 phase 为 queued/analyzing，ready 含 suggestion（rows、columns、order、page_slots、crop_margins_mm、width_mm、height_mm、confidence、source），其他终态含 reason/error。改模式产生新 ID，迟到旧任务不能覆盖新建议；手动应用使旧建议失效。

`POST layout` 表单 `method=auto&analysis_id=...` 必须等于当前 ready 且可靠建议的 ID，否则 409；不采用客户端上传的自动方案。手动表单 `method=manual`、rows/columns（1–24）、order（row/column）、crop_left_mm/right/top/bottom：裁边为非负有限数，每页裁后宽高至少 1mm，无新增 100mm cap；格数必须覆盖全部页面，每页一次，尾部补空槽。不漏页、不重复、不拉伸。混尺寸按各列最大可见宽、各行最大可见高左上对齐；整行／整列空槽按所有裁后页面最大宽／高保留真实空白网格。返回 `{layout:{rows,columns,order,page_slots,crop_margins_mm,width_mm,height_mm,source},width_mm,height_mm}`。

拼版是独立虚拟位置元数据，瓦片仅对相交原页 `get_pixmap(annots=True)` 并等比例贴到有限画布；文字、颜色、图片和可见标注保留，不写新 PDF、不经 PDF→PLT，不继承旧输出 PDF 的 5080mm 单页限制。用户裁边会改变可见区域，但不更改原文件。低置信、异常或新建议仅改变分析状态，不擅自改变已显示页／已应用布局。

新 `pdf-projection-layout` RQ 队列与 `projector-layout-worker` 只负责此分析，旧队列、Worker、计费及 health 定义不变。沿用旧分析预算：队列最多 20 个等待任务（满时 429），每次**执行**最长 90 秒，排队时间另计；入队记录 TTL 3600 秒作失联兜底。提交需 only-queue=pdf-projection-layout、idle/busy、heartbeat 未超过 RQ worker_ttl（默认 420 秒）的专用消费者，否则 503 保留原页。等待时消费者失联会标失败并取消新队列的旧任务；missing／failed／stopped／无结果完成／执行超时也明确失败可重试，避免无限 analyzing。90 秒分析在锁外执行，发布前重查身份、会话有效及最新 ID；离页删除、手动应用或改模式后不能复活／覆盖旧状态。新 worker 固定命令 `python -c 'from app.projection_layout import worker_main; worker_main()'`；新入口使用独立 Compose project、Redis 和磁盘卷，见下方启动模板。

上传先建立 3600 秒失联兜底；成功 renew 对有效私有会话原子更新到此次成功续期后 3600 秒，返回 `{document_id,expires_in:3600}`。GET 取图不隐式续期。前台每 5 分钟心跳（无操作也续期），失败 30 秒重试并提示；onHide 暂时后台暂停且不删除，回前台恢复；onUnload 离开投影页、清除或合法更换文件时主动 DELETE。常亮请求可能增加耗电且受系统限制；这些为已披露 AI 工程机制。强退／失联不保证即时通知退出，最后成功续期 60 分钟无新续期失效；已删／过期／异主／非法 ID 均 404，不能复活。写入续期失败 503 保留旧期限；删除成功 204 不删除用户原 PDF 或其他转换文件。

旧本地 prototype 会话位于 `PLT_TEMP_FOLDER/projection-documents`；独立入口候选使用 `PROJECTOR_ROOT` 下受预算目录，不自动迁移旧会话。prototype 的“64 个有界分片 POSIX 文件锁”已由投影自有 SQLite 账本取代：会话、lease、限流与空间预算都在同一 SQLite 里用短事务串行化；事务从不在解析、渲染、Redis、鉴权或读取请求体期间持有。清理不是精确后台 cron：reconcile 在请求时机会式清理已完成删除、过期与失联 lease 的文件，访问到期会话立即按 404 清除。持续前台续期可使用超过 60 分钟并持续占用临时存储，不承诺永久保存或永不过期。多 API 实例必须共享同一磁盘卷（SQLite 单写者），不再依赖共享文件锁。实际手机投屏、尺量、外部 CAD、扫描样本、长时性能与目标环境仍需独立验收。

本地投影专项：`.venv/bin/python -m unittest discover -s tests -p 'test_projection*.py'`；部署模板另用 `.venv/bin/python -m unittest discover -s tests -p 'test_projector_manifests.py'`（使用测试用本地 Unix socket 与 mock Docker CLI，不连接真实 Docker）；新独立镜像从批准的 immutable 旧基线只增加任务 COPY allowlist；旧 factory 撤回本任务 projection 注册，投影仅由 `run_projection:app` 提供。逐文件核对其他转换源码和依赖保持，不重建当前工作区的旧转换镜像。

本轮 B0–B8 行为变化（以当前实现为准）：B0 单账户单文档，新上传先删除该账户旧文档并撤掉其仍在排队的分析，旧会话立即 404 且不能复活。B1 解析名额在收完请求体后才占用：慢速上传只先预留 staging 空间，收 body 期间不占 global parser；body 收完后 parser 忙返回 429 并释放 staging 与 lease。B2 切换 `metadata_mode` 会换新 analysis_id 并移除旧排队任务，且每账户同时最多 1 个分析，已有分析进行中再提交返回 429 `projection_analysis_owner_busy`。B5 并发与异常不返回 500：并发删除后的 lease／生成号不匹配返回 404（`projection_not_found`）或 409（`projection_lease_lost`／`projection_result_stale`），Redis／RQ 故障返回 503 `projection_worker_unavailable` 并带 `Retry-After`。B6 分析状态变化不再提升会话版本：分析失败、改模式与迟到状态只更新分析字段，进行中的取图 lease 不被判 stale，只有真正应用排版（POST layout）才提升版本。B7 子进程只有显式 ValueError 文案原样返回客户端（`error.json` 带 `public: true`，最多截断 500 字），其余含 MuPDF／内部 traceback 一律换成固定中文文案并只写日志。B8 非旋转拼版只在跨页接缝的重叠像素窄条做透明合成以保留两侧墨迹，其余像素保持 MuPDF 原色字节。

## Docker

```bash
docker compose up --build
```

默认部署为 1 个 API、1 个正式转换 Worker、1 个排版分析 Worker 和 1 个 Redis。两个 Worker 各自限制为 1 CPU / 1GB 内存；提高副本数或调整资源限制前，需要同步评估主机 CPU、内存和队列容量。文件只用于临时处理，不进入缝纫记忆主业务数据库。

### 生产部署

API、正式转换 Worker、排版分析 Worker 必须使用**同一 Git 提交构建的同一发布版本**，三个组件更新到同版并通过健康检查后，才可发布依赖新排版协议的前端。源文件 job、预览与算法缓存由这三个组件共同读取和处理，跨版本混跑会让请求字段、参与页集合、外围保留几何及缓存算法版本不一致；不能只更新 API 或其中一个 Worker。新前端会拒绝缺少 `page_indexes` 的旧 API 排版响应，不能用旧服务承接新前端。下述部署脚本的暂停接单、双队列排空与回滚流程保持不变；发布顺序检查不替代该流程。

转换服务必须作为独立的 `plt-converter` Compose 项目运行。它可以和其他项目共用一台 Docker 主机，但不共享 Compose 配置、环境文件、网络或数据卷。

```bash
cp /opt/plt-converter/.env.production.example /opt/plt-converter/.env.production
# 设置主后台地址、服务密钥和指标密钥

(
  set -Eeuo pipefail
  cd /opt/plt-converter
  build_context="$(mktemp -d /tmp/plt-converter-build.XXXXXX)"
  trap 'rm -rf -- "$build_context"' EXIT
  git pull --ff-only origin main
  test "$(git rev-parse HEAD)" = "$(git rev-parse refs/remotes/origin/main)"
  git archive --format=tar HEAD | tar -xf - -C "$build_context"
  export PLT_BUILD_CONTEXT="$build_context"

  docker compose \
    --env-file /opt/plt-converter/.env.production \
    -f /opt/plt-converter/compose.production.yaml \
    -f /opt/plt-converter/compose.production.build.yaml \
    config

  docker compose \
    --env-file /opt/plt-converter/.env.production \
    -f /opt/plt-converter/compose.production.yaml \
    -f /opt/plt-converter/compose.production.build.yaml \
    up -d --build
)
```

首次部署完成后，后续更新统一使用一键部署脚本：

```bash
cd /opt/plt-converter
./scripts/deploy-production.sh
```

脚本固定使用 `plt-converter` 项目及本仓库生产配置，会要求当前分支为 `main`、已跟踪文件没有未提交修改，并确认本地 HEAD 与远端 `origin/main` 完全一致；未跟踪的运维文件可以保留，因为镜像始终从当前 Git 提交生成的隔离临时上下文构建，不会把这些文件带入镜像。随后脚本会校验端口归属、服务边界和 Redis 状态。构建专用配置只在构建镜像时叠加，日常 `ps`、`logs`、`start`、`restart` 等运维命令只需使用 `compose.production.yaml`，无需设置临时构建目录。新镜像在线构建完成后，脚本会暂停 API 接收新任务，等待正式转换与排版分析两个队列全部排空，再更新 API、正式转换 Worker 和排版分析 Worker，并执行有超时限制的健康检查及主后台服务密钥验证。部署失败、队列等待超时或收到 `INT` / `TERM` 信号时，会尝试重新开放原 API 或恢复更新前的 API 和 Worker 镜像；若上一版还没有独立排版 Worker，回滚会移除本次新增的排版 Worker。恢复后重新执行健康检查及主后台连接验证，恢复不完整时会明确报错并要求人工检查。脚本不会操作其他 Compose 项目、重建 Redis 或删除数据卷。

转换 API 只绑定宿主机 `127.0.0.1:8091`，供宿主机 Nginx 反向代理；转换服务 Redis 不开放宿主机端口。
生产数据卷使用固定名称 `plt_converter_redis_data` 和 `plt_converter_temp`，不会随 Compose 项目名变化。
镜像构建默认使用腾讯云 PyPI；可在 `.env.production` 中设置 `PLT_PIP_INDEX_URL=https://pypi.org/simple` 覆盖。

## 防护配置

```text
PLT_QUEUE_MAX_PENDING=20
PLT_JOB_TIMEOUT_SECONDS=90
PDF_TO_PDF_JOB_TIMEOUT_SECONDS=210
PLT_JOB_RETENTION_SECONDS=1800
PLT_RATE_LIMIT_PER_MINUTE=3
PLT_PREVIEW_RATE_LIMIT_PER_MINUTE=12
PLT_UPLOAD_RATE_LIMIT_PER_MINUTE=10
PLT_UPLOAD_IP_RATE_LIMIT_PER_MINUTE=30
PDF_LAYOUT_OPTIMIZE_RATE_LIMIT_PER_MINUTE=6
PDF_LAYOUT_QUEUE_NAME=pdf-layout-analysis
PDF_LAYOUT_QUEUE_MAX_PENDING=20
PDF_LAYOUT_OPTIMIZER_TIMEOUT_SECONDS=90
PDF_LAYOUT_OPTIMIZER_MAX_CROP_MM=15
PDF_LAYOUT_OPTIMIZER_CROP_STEP_MM=0.1
PDF_LAYOUT_OPTIMIZER_MAX_BLANK_CELLS=8
PLT_USER_MAX_ACTIVE_JOBS=2
PLT_MAX_UPLOAD_MB=20
PLT_MAX_POINTS=500000
PLT_MAX_PATHS=100000
PLT_MAX_DIMENSION_MM=10000
PLT_MAX_OUTPUT_PAGES=80
PLT_MAX_COMMANDS=250000
PLT_MAX_TEXT_CHARS=100000
PDF_MAX_TOTAL_PIXELS=200000000
PDF_MAX_DRAWINGS=250000
PDF_MAX_OUTPUT_SEGMENTS=300000
PLT_METRICS_TOKEN=<生产环境生成的随机令牌>
WX_BACKEND_URL=https://api.fengrenjiyi.com
CONVERSION_SERVICE_TOKEN=<与主后台相同的服务密钥>
```

健康检查为 `/health`、`/health/redis`、`/health/worker`；总健康状态和 Worker 健康状态都要求正式转换、排版分析两个队列各有活跃消费者。指标接口为 `/api/v1/plt/metrics` 和 `/api/v1/pdf/metrics`；未设置 `PLT_METRICS_TOKEN` 时接口不启用，启用后请求必须携带 `X-Metrics-Token`。Redis 使用 128MB、AOF 和 `noeviction`，内存写满时会明确拒绝新任务，因此生产监控应同时关注 Redis 内存、接口 503 和队列拒绝数。

## 验证

按改动选择下列现有命令，从仓库根目录运行。单元测试使用本地 `.venv` 和 `requirements-dev.txt`；`compileall` 检查语法，`bash -n` 检查部署脚本语法，`docker compose config` 只解析本地 Compose 配置。它们不证明 Redis 队列、真实转换文件、主后台回调或目标环境已通过。

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
.venv/bin/python -m compileall -q app worker.py
bash -n scripts/deploy-production.sh
docker compose config
```

只修改相关 Python 行为时可用 `.venv/bin/python -m unittest discover -s tests -p 'test_pdf_options.py'` 之类的实际测试文件先做定向验证，再按风险扩展。仅改文档时核对链接、`git diff --check` 与 `git status --short` 即可。验证报告需写明实际运行的命令、环境、结果和未验证边界；生产读取、预检或部署需要当前任务对具体操作的明确授权。

## 投影独立启动与发布模板（本轮仅静态验证）

`compose.projector.yaml` 是独立 `fengrenjiyi-projector` project，只有 projector-api、projector-layout-worker、projector-redis。API 与 Worker 共用显式 `PROJECTOR_IMAGE`，8092 仅供独立投影；8091 旧转换不变。旧 `compose.production.yaml` 与 `scripts/deploy-production.sh` 继续只部署旧栈，**不能启动投影**；将来配套发布必须另走新投影入口、确认身份后端与网关精确 `/api/v1/pdf/projection` 路由。其他 PDF/PLT 路由保留旧栈。两栈健康分别记录，不能用新健康掩盖旧 health 503。

独立投影 Compose 在 API 与专用分析 Worker 中显式设置 `PDF_LAYOUT_OPTIMIZER_MAX_BLANK_CELLS=8`，修正未配置时沿用旧镜像默认4的限制。需要5–8个空槽的保序网格现在可以参与自动接缝比较；仍由原接缝得分、置信阈值和搜索顺序决定结果，不强制某个行列，也不保证任意PDF都能可靠拼接。该变量沿既有白名单传入分析子进程；90秒执行预算、队列上限20、裁边和页数等其他规则不变。此覆盖只属于 `compose.projector.yaml`，不修改共享分析器源码、旧8091栈或 `compose.production.yaml` 的配置。 该配置不会重算已保存的建议：同一文档、同一mode的ready结果仍会幂等复用，“一键排版”不保证重新分析。旧ready结果需重新上传文件建立新会话后才能按新上限分析；正常换源会按既有单纸样规则释放旧文档。

当前 gate `PROJECTOR_ENABLED=0`；容量、限流以及待确认工程并发/执行 deadline 全部来自显式 `PROJECTOR_POLICY_FILE`，产品示例列出用户2026-10-14批准的2会话、40MiB源文件、global2GiB、free floor2GiB、owner5/IP30固定UTC minute上传及tile3/s burst5；示例不作为应用默认。启用时后端严格校验完整 policy；disabled readiness 返回 503 属预期。不得将本次模板称已运行或已部署。

`.env.projector.example` 的 CPU、内存、PID、HTTP 线程/timeout 数值全是 **AI 建议 / 待确认的非生产工程验证预算**，不是已批准线上值；需实测子进程预算和身份延迟并确保 HTTP timeout 足够容纳 parse/tile deadline + 身份超时。`GLOBAL_DISK_BYTES` 是全部落盘应用账本预算，`FREE_FLOOR_BYTES` 是宿主余量；容器 `mem_limit`/子进程内存是 RAM，两者不可互称。Docker `projector-data` 是磁盘卷，普通 volume 不提供硬 filesystem quota；生产硬盘隔离需单独授权的专用 filesystem/project quota。源、multipart、staging、jobs、SQLite state 都只能写该新根；8MiB `/run/projector` 仅 Gunicorn 控制 scratch，禁止 PDF。Redis 独立 RAM 限额/noeviction，不启 AOF/RDB、不持久化排版任务；重启后失联分析明确失败可重试，源会话仍在磁盘。日志轮转是单独宿主日志预算。

镜像只用本机已有批准 `sha256:60ac62f0...` 或 `sha256:7801cc5c...` immutable base ID。`PROJECTOR_BASE_ID` 记录该ID；`PROJECTOR_BASE_IMAGE` 必须是唯一任务专用 `localhost:1/fengrenjiyi-projector-base:review-...` alias。显式 `--prepare-base-local --docker-socket /absolute/local/socket` 先核对ID，再给新alias；已有alias指向不同ID则拒绝覆写，不重打旧canonical tag。不可把bare sha256 ID传给FROM。构建必须用指定本机socket并显式默认Docker driver：`docker --host unix:///absolute/local/socket build --builder default --pull=false --network=none --build-arg PROJECTOR_BASE_IMAGE=VERIFIED_TASK_ALIAS -f Dockerfile.projection -t EXPLICIT_CANDIDATE CLEAN_CONTEXT`；不得使用自定义远程/docker-container builder，Compose build模板仅用于配置审阅，实际本机构建按此命令；fallback registry仅localhost:1不可访问，禁止远端拉取。`Dockerfile.projection` 不装依赖，仅 COPY 明确任务文件与 init 撤回注册；旧转换继承模块保持，既有 `app/services/` helper COPY（`plt_parser.py`、`pdf_renderer.py`、`number_tokens.py`、`pdf_metadata.py`）继续保留。本轮兼容修复只新增投影私有目录 `app/_projection_pdf/`，从仓库 `app/services/pdf_layout_optimizer.py`、`app/services/pdf_to_plt.py`、`app/services/pdf_metadata.py` 三个来源 COPY；原路径中的旧 optimizer 与 PDF→PLT 模块不覆盖。旧16模块的保持结论是相对修复前 PR16 的镜像组装不新增差异，不能表述为候选镜像旧16文件与最原 base 全部相同。`--prepare-context` 的 allowlist 包含私有 loader 及上述三个同源来源，将名单复制到全新 `/private/tmp/projector-build.*`，禁止用整个 checkout 作增量 context。API/Worker 显式相同 image 不依赖目录名或 `-p` 推导 tag。Compose `pull_policy: never`；未来生产需先将批准 base/candidate/Redis 供应链放到获授权 registry 并锁 digest，脚本不会 fallback 拉未知 base，也不重打旧 canonical tag。

```bash
# 静态模板，不启动/构建/重启任何容器。先复制 env 示例，填写实际非生产路径。
bash scripts/deploy-projector.sh --offline --target-scope local-review --env-file /private/tmp/projector-review.env
bash scripts/start-projector-local.sh --dry-run --env-file /private/tmp/projector-review.env
# 可选，仅准备新任务源码 context，不调用 Docker：
bash scripts/deploy-projector.sh --offline --target-scope local-review --env-file /private/tmp/projector-review.env --prepare-context /private/tmp/projector-build.review-new
# Compose config 是纯配置解析；policy 文件必须存在，可用空 policy 检查 disabled 模板。
docker compose -p fengrenjiyi-projector --env-file /private/tmp/projector-review.env -f compose.projector.yaml config
```

未来获具体授权后的 runbook：核对本机 approved images、clean context、policy/gate 与旧容器保护指纹；只 build 新 projector-api candidate，检查 API/Worker 相同 image；只以白名单启动新 Redis/new Worker/new API，API 与 Worker 仅依赖 Redis healthy，避免循环 readiness。等 `/health/live`、完整 `/health/readiness` 与 only-new-queue live consumer；再进行真实身份 upload/tile/layout/renew/delete 验收和旧栈保护复查，才能按另外授权开放入口/网关。更新与回滚只涉及这 3 个新服务，保留 projector-data 与 schema 兼容，记录前版 image/env/schema；不提供自动 down/prune/旧服务排空或停止。脚本支持显式 `--apply-local`（本地入口为 `--start-local`）和 `--docker-socket /absolute/local/socket`；强制 Unix socket 覆盖 Docker context/DOCKER_HOST，`up --force-recreate --no-build --pull never --wait` 仅白名单 3 新服务，随后核查完整 readiness 与 only-new-queue consumer。policy enabled=1 的本地启用需显式填写完整待验配置。本轮未执行此模式；生产/远程及自动回滚均不支持。

2026-10-14已批准额度与内部账本的关系：40MiB仅owner源PDF额度，global2GiB还包括额外写入。内部metadata上界344064B（200页字段＋最多576格＋65536系统余量），upload预留21MiB body＋两份metadata＋65536，operation预留两份metadata＋PNG上界12065536＋65536。另预留render磁盘pool为2×12819200B，计入global，upload/analysis不可占，活动取图可用；该pool不代表RAM，也不能保证外部writer/物理满盘时成功。SQLite按global/16向block对齐预算，DB/rollback双份＋65536计入system；renew仅更新SQLite期限，不改大JSON。内部derive是AI工程保护，不把两份20MiB源文件说成总共40MiB硬盘。限流owner/IP表有界存SQLite，新Redis仅RQ；Redis故障不阻止renew/delete。新并发1/2/1、parse90s/tile30s、CPU30s/AS512MiB仍为AI仅本地验证值，Linux真实文件验收与生产预算待定。

API容器示例内存上限调整为2GiB（AI仅本地验证、非生产值），为最多parser1＋renderer2、各child AS512MiB之外的父Gunicorn、8线程及结果body留余量；容器上限不是启动即预留/实占2GiB，也不代表三个child同时会各占满512MiB。worker1GiB示例保持；真实峰值、OOM与renew可用性仍须Linux集成验证。

新投影栈的私有PID namespace由 `projector-redis`（init:true）作为anchor，API/Worker使用 `pid: service:projector-redis`。三个新服务中的PID＋/proc startTicks因此可互见，避免共享SQLite误把另一个新容器的活supervisor/child判死并提前回收空间/并发lease。它不共享宿主或任何旧服务PID namespace；CPU、RAM与PIDs cgroup上限仍逐容器独立。更新显式 `--force-recreate` 仅重建白名单三个新服务，保证Redis换新namespace后API/Worker引用新anchor，保留projector-data；Redis任务仍是ephemeral，重建导致分析失败可重试。anchor PID1退出会终止该namespace内任务，更新有新栈短暂不可用窗口；旧栈不停止/不重建。PID一致性、跨容器活lease及anchor退出后的实际Docker生命周期必须由runtime Owner验证，静态配置通过不替代此验收。

API 与 Worker 容器以固定非 root uid:gid `10001:10001` 运行（`read_only` 根文件系统不变）；`Dockerfile.projection` 只用 `COPY --chown=10001:10001` 复制 `docker/projector-data/.keep` 占位目录，让新建的 `projector-data` 命名卷首次挂载即归属该 uid。既有 root 属主的旧卷不会自动迁移，需人工 `chown -R 10001:10001` 该卷后容器才能写入，否则 API/Worker 会因无写权限失败；本机 candidate `sha256:c30bb5dac1588f2583b3b96ebe2da563c4e0e843f93f09e9e0a17da25dc1c2a8` 已完成真实 Docker 验收：旧验收卷属主修正后，API／Worker 的 SQLite 增量迁移与 healthy 状态、PLT 上传／取图／续期／删除链路通过；该证据仅限本机，不代表生产卷迁移或生产验收，也不代表可从任意通用 base 重新构建。解析/取图/分析子进程只继承 PATH、LANG/LC_*、PYTHON*、PDF_MAX_TOTAL_PIXELS、PDF_MAX_DRAWINGS、PLT_MAX_DIMENSION_MM 与 PDF_LAYOUT_OPTIMIZER_* 白名单环境，`CONVERSION_SERVICE_TOKEN`、`PROJECTOR_REDIS_URL`、`WX_BACKEND_URL` 等密钥与身份/Redis 地址不得进入子进程。

本轮 B3／B4 部署变化：B3 子进程环境白名单与容器固定非 root uid:gid `10001:10001`（环境白名单见上一段）。B4 `scripts/deploy-projector.sh` 部署前校验：凡会被 compose 插值或 env 文件定义、且已在 shell 中导出的 `PROJECTOR_*` 变量一律拒绝（compose 插值优先于 `--env-file`，否则可绕过校验，含 env 文件省略而由 compose 取默认的项）；`PROJECTOR_WX_BACKEND_URL` 必须是纯 ASCII 且不含 `%`，host 只允许 `localhost`、`host.docker.internal`、不含点的单段主机名（compose 服务名）或回环/RFC1918 私网 IPv4，其余（含 `fengrenjiyi.com` 及其子域、公网主机与 IP、非 ASCII 或百分号别名）一律拒绝；`PROJECTOR_BIND_ADDRESS` 仅允许 `127.0.0.1` 或 RFC1918 私网 IPv4（拒绝 `0.0.0.0` 与公网地址）；`PROJECTOR_POLICY_FILE` 必须存在且用同一逐行规则解析，只允许 `PROJECTOR_` 加 `app/projection_store.py` 中 `FIELDS` 的键，`http_proxy`、`HTTPS_PROXY`、`NO_PROXY`、`WX_BACKEND_URL` 等代理或其他变量一律拒绝；`--prepare-context` 只把批准的源码名单规范化复制到全新的 `/private/tmp/projector-build.*`；除 `--apply-local` 外所有模式要求 `PROJECTOR_ENABLED=0`，`--apply-local` 要求 `PROJECTOR_ENABLED=1` 并拒绝关闭 gate 时启动。

同机生产准备材料见 [投影离线准备](docs/deployment/projector-shared-host.md)：仅校验与脱敏计划，不执行部署、不开放 gate；原 local-review 部署脚本保持不变。

### 投影上传限流的可信代理 IP

仅上传限流使用 `PROJECTOR_TRUSTED_PROXY_CIDRS`：逗号分隔的明确 IPv4/IPv6 IP 或规范 CIDR，推荐精确 `/32`、`/128`。默认空表示完全忽略转发头，使用真实连接 IP；不自动信任私网、Docker 网关或 CDN。只有真实连接 peer 命中允许表，才接受 `X-Forwarded-For` 中单个合法 IP；未配置、不可信 peer、缺失／非法／多地址头均回退连接 IP，不因此拒绝普通请求。IPv4-mapped IPv6 与 IPv4 归一化为同一限流 IP。不读取 `Forwarded` 或 `X-Real-IP`；鉴权仍由后台 identity 确认，不使用这些 IP 识别账户，也不修改 host/proto。

Nginx 模板用 `$remote_addr` 覆盖 XFF，丢弃客户端自带头。必须在未来获授权部署时核对容器实际看到的连接 peer 后显式填写配置；当前未连接生产核对。CDN／多层网关须另行核验前置 Nginx realip 的可信来源和真实客户端地址，本轮不推断可信链。部署前不要直接复制任意示例私网地址。

**AI 自行添加规则（工程输入保护）**：配置中非法 IP/CIDR、空列表项、带主机位的非规范 CIDR、IPv6 scope ID、全网 `/0`（含映射的全 IPv4 范围，以及多个 CIDR 联合覆盖全网）导致启动／生产离线检查失败，错误不输出原配置值，避免误配为任意来源可伪造限流 IP。生产离线准备要求该字段显式非空；本地可留空。每账户每 UTC 分钟 5 次、每 IP 每 UTC 分钟 30 次、瓦片每秒 3 次及 burst 5 均保持；上传失败请求仍按现有规则计入限流。生产预算仍待明确填写，`PROJECTOR_ENABLED=0` 与前端发布门禁继续关闭。

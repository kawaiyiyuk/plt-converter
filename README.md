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

## Docker

```bash
docker compose up --build
```

默认部署为 1 个 API、1 个正式转换 Worker、1 个排版分析 Worker 和 1 个 Redis。两个 Worker 各自限制为 1 CPU / 1GB 内存；提高副本数或调整资源限制前，需要同步评估主机 CPU、内存和队列容量。文件只用于临时处理，不进入缝纫记忆主业务数据库。

### 生产部署

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
PDF_LAYOUT_OPTIMIZER_MAX_BLANK_CELLS=4
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

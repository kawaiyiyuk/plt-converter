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
- A4/A3/A1/A0/Letter 分页 PDF 生成
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
  -F "margin_mm=0" -F 'page_slots=[0,1,null,3]' \
  http://127.0.0.1:8090/api/v1/pdf/jobs
```

提交接口返回 `job_id` 和 `status`。任务状态包括 `queued`、`processing`、`done`、`failed`、`cancelled`。查询、取消、下载和预览图请求必须使用提交时相同的 `X-Client-Key`。

```text
POST /api/v1/plt/jobs
GET  /api/v1/plt/jobs/<job_id>
DELETE /api/v1/plt/jobs/<job_id>

POST /api/v1/pdf/preview
POST /api/v1/pdf/jobs
GET  /api/v1/pdf/jobs/<job_id>
DELETE /api/v1/pdf/jobs/<job_id>
GET  /api/v1/pdf/previews/<preview_id>/<page>.png
```

完成任务后返回 `pdf_path`、`plt_path` 或 `preview_id/pages`。Redis 不可用返回 503；队列、用户容量或限流拒绝返回 429 和 `Retry-After`。`page_slots` 中的 `null` 表示保留空白格。图片 PDF 的降级描边精度不等同于矢量 PDF。

## Docker

```bash
docker compose up --build
```

默认部署为 1 个 API、1 个 RQ Worker 和 1 个 Redis。转换并发由 Worker 副本数控制；提高副本数前需要同步评估 CPU、内存和队列容量。文件只用于临时处理，不进入缝纫记忆主业务数据库。

### 与 Appsbox 合并部署

服务器目录为 `/opt/appsbox-backend` 和 `/opt/plt-converter` 时，可以在保留两个独立 Git 仓库的同时，将它们作为同一个 `appsbox` Compose 项目运行：

```bash
cp /opt/plt-converter/.env.production.example /opt/plt-converter/.env.production
# 将示例令牌替换为 openssl rand -hex 32 的输出

docker compose \
  --project-directory /opt/appsbox-backend \
  --env-file /opt/appsbox-backend/.env \
  --env-file /opt/plt-converter/.env.production \
  -f /opt/appsbox-backend/compose.yaml \
  -f /opt/appsbox-backend/compose.prod.yaml \
  -f /opt/plt-converter/compose.appsbox.yaml \
  config
```

转换 API 只绑定宿主机 `127.0.0.1:8091`，供宿主机 Nginx 反向代理；转换服务 Redis 不开放宿主机端口。

## 防护配置

```text
PLT_QUEUE_MAX_PENDING=20
PLT_JOB_TIMEOUT_SECONDS=90
PLT_JOB_RETENTION_SECONDS=1800
PLT_RATE_LIMIT_PER_MINUTE=3
PLT_PREVIEW_RATE_LIMIT_PER_MINUTE=12
PLT_UPLOAD_RATE_LIMIT_PER_MINUTE=10
PLT_UPLOAD_IP_RATE_LIMIT_PER_MINUTE=30
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
```

健康检查为 `/health`、`/health/redis`、`/health/worker`。指标接口为 `/api/v1/plt/metrics` 和 `/api/v1/pdf/metrics`；未设置 `PLT_METRICS_TOKEN` 时接口不启用，启用后请求必须携带 `X-Metrics-Token`。Redis 使用 128MB、AOF 和 `noeviction`，内存写满时会明确拒绝新任务，因此生产监控应同时关注 Redis 内存、接口 503 和队列拒绝数。

## 验证

```bash
PYTHONPATH=. .venv/bin/python tests/test_metadata.py
PYTHONPATH=. .venv/bin/python tests/test_pdf_renderer.py
PYTHONPATH=. .venv/bin/python tests/test_job_queue.py
PYTHONPATH=. .venv/bin/python tests/test_plt_parser_limits.py
PYTHONPATH=. .venv/bin/python tests/test_pdf_options.py
PYTHONPYCACHEPREFIX=/tmp/plt-converter-pycache python3 -m py_compile app/*.py app/services/*.py worker.py
docker compose config
```

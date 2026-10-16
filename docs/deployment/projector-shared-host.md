# 纸样投影：同机生产部署的离线准备

用户已选择投影与既有 PDF/PLT 转换共用服务器。本包只准备可复核文件，不部署、不连接生产、不开放体验／正式入口。`scripts/deploy-projector.sh` 仍只支持 local-review，拒绝生产身份；不得用本包绕过该保护。离线输入通过不等于生产 preflight、健康检查、上线或真机验收通过。

## 文件与运行方式

复制 `.env.projector.production.example` 和 `.env.projector.production.policy.example` 到私密位置，手动填写所有空值。前者用于准备器的连接／镜像／容器工程预算，`PROJECTOR_POLICY_FILE` 必须为后一份 policy 文件的绝对路径。policy 文件只允许业务与处理预算白名单，禁止 proxy 等其他环境变量。不要将真实凭证提交 Git；准备器只读取，不复制或输出 token。支持的输入是无引号的字面 `KEY=value`，不支持 shell export、插值、空白值内容、行内注释或重复键。

```sh
python3 scripts/prepare-projector-production.py --env-file /absolute/private/production.env --check
python3 scripts/prepare-projector-production.py --env-file /absolute/private/production.env
```

缺失或含占位值时只报告字段名，退出码为 2；输入有效才返回 0。`--check` 显示状态及必要的时长配置复核警告，完整模式显示脱敏计划。所有 shell `PROJECTOR_*` 必须先移除，避免将来 Compose 优先读取 shell 而绕过文件复核。准备器只用 Python 标准库，静态抽取源码 Policy，无 app 初始化、Docker、SSH、HTTP、进程启动或服务探测。没有输出部署文件、复制密钥、启动／回滚服务的能力。

两份生产示例不是现成部署 env。`PROJECTOR_PUBLIC_BASE_URL` 仅用于离线校验网关 HTTPS origin，当前 compose/app 不读取；不得据此声称客户端已配置。生产身份后端 HTTPS origin 与 token 必须显式提供，token 匹配仍需未来人工核验。镜像仅接受不可变 registry/repository@sha256 digest；实际构建、分发、拉取和来源证明均待授权人工完成，不能填本地 review tag 冒充。

## 固定拓扑与共享边界

沿用 `compose.projector.yaml`：project 为 `fengrenjiyi-projector`，只有 `projector-api`、`projector-layout-worker`、`projector-redis` 三个新服务；API 仅发布 `127.0.0.1:8092`。API 和 worker 同一镜像，独立磁盘 volume `projector-data` 挂载 `/var/lib/projector`，SQLite lease 依赖两者共享新 Redis 的 PID namespace；Redis 不发布宿主端口、独立无持久队列、noeviction。不得拆散 PID 语义或复用旧转换 Redis／volume。

两栈同机仍共享物理 CPU、RAM 和磁盘。应用记账不是文件系统硬配额；空闲 floor 不能保护其他写入者。示例不提供生产 CPU、RAM、PID、HTTP 线程、并发、子进程预算或 Redis 内存默认值；全部须显式选择，宿主余量和既有 PDF/PLT 峰值仍待核实。Redis 容器内存须大于 maxmemory 以留开销，具体余量待选。本包 Nginx 示例采用 120 秒无活动超时，它不是应用统一 wall cap，也不能保证请求总时长。HTTP、parse 或 tile 显式预算超过 120 秒时，准备器返回 `timeout_review_required=true` 和配套人工审定警告，输入仍可通过离线校验；不能因此声称网关与应用预算已经兼容。生产预算与网关设置差异须配套审定。

HTTP body 21MiB／源文件 20MiB 沿用。policy 的已确认额度仍为 owner 文档账本 2、源 40MiB、global 2GiB、free floor 2GiB、owner 上传每 UTC minute 5 次／IP 30 次、tile 每秒 3 次 burst 5。账本 2 不改变客户端／服务端换新时替换旧纸样、同账号仅一份生效规则。PLT 临时派生 guard 不计入源额度，生产值也须显式复核。准备器拒绝修改上述确认值，改产品规则需另行用户决定。

## 网关准备与未来授权工作

`config/nginx/projector-location.conf.example` 只匹配 `/api/v1/pdf/projection/` prefix，转发至 loopback8092，保留完整 URI 和 Authorization，关闭请求／响应 buffering，body 21m。其余既有 PDF/PLT 路由继续 8091。示例必须放进已有 TLS server 后再由人工复核 location 优先级、TLS、身份请求、上传与流式取图；本包不编辑宿主 Nginx、不重载、不核验域名证书。

复用现有 `Dockerfile.projection` 增量 COPY 思路时，必须另外取得可追溯生产基础镜像和依赖来源证明。local-review 的两个已批准本机 image ID 只是本机证据，不是生产镜像来源许可。旧部署器和 compose 均原样保留，compose 中 local-review 提示不代表现已获得生产执行授权。

将来明确获得对应生产权限后，由人工分别完成：

1. 核实宿主容量、选定工程预算、构建／分发并确认 app 与 Redis digest，保证 API／worker 版本一致。
2. 核验身份 token 与后端匹配、网关及 TLS；安全保存私密 env，避免 `compose config` 输出泄露 token。
3. 制定只针对该新 project 的三服务同版 force-recreate、健康与回滚计划。本准备阶段 backend `PROJECTOR_ENABLED=0` 时 worker 不运行、readiness 按设计返回 503；这不作为服务故障，也不能声称直接 up --wait 会健康。未来获得私有后端运营的明确授权后，须先审定镜像与配额，再人工显式设 backend=1 才具备 worker/readiness 的运行前提；这不开放客户端。frontend 体验／正式 release gate 仍保持关闭，直至验收并获得另行开放决定。
4. 服务替换或无持久 Redis 重启可能中断分析；用户原始 PDF／PLT 不改写。区分临时投影会话和原文件，不承诺跨版本 session 恢复。回滚记录旧 digest／policy、三服务同步版本和 volume 兼容性；不得删除 volume 来冒充回滚。
5. 分别记录新栈健康、旧转换健康及网关证据。手机投屏实体横／纵尺验收按用户实际完成范围独立记录，本离线包不虚构通过。

无新增用户业务规则。AI 自行添加的工程输入保护：严格字面 env、拒绝 shell 覆盖、HTTPS origin、不可变镜像引用、显式生产预算、资源一致性检查及 Nginx 示例时长差异提示，用于让准备材料可复核；不等于预算获批或 gate 开放。

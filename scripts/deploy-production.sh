#!/usr/bin/env bash
set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
readonly ENV_FILE="$PROJECT_DIR/.env.production"
readonly COMPOSE_FILE="$PROJECT_DIR/compose.production.yaml"
readonly BUILD_COMPOSE_FILE="$PROJECT_DIR/compose.production.build.yaml"
readonly PROJECT_NAME="plt-converter"
readonly API_IMAGE="plt-converter-api:production"
readonly WORKER_IMAGE="plt-converter-worker:production"

compose=(
  docker compose
  -p "$PROJECT_NAME"
  --env-file "$ENV_FILE"
  -f "$COMPOSE_FILE"
)

build_compose=(
  "${compose[@]}"
  -f "$BUILD_COMPOSE_FILE"
)

rollback_enabled=0
replacement_started=0
old_api_image=""
old_worker_image=""
build_context=""
candidate_api_image=""
candidate_worker_image=""

log() {
  printf '\n[%s] %s\n' "$(date '+%F %T')" "$*"
}

fail() {
  printf '错误：%s\n' "$*" >&2
  exit 1
}

container_image_id() {
  local service="$1"
  local container_id
  container_id="$("${compose[@]}" ps -q "$service")"
  if [[ -n "$container_id" ]]; then
    docker inspect "$container_id" --format '{{.Image}}'
  fi
}

verify_runtime_health() {
  local failed=0
  curl --fail --silent --show-error --connect-timeout 3 --max-time 10 http://127.0.0.1:8091/health || failed=1
  printf '\n'
  curl --fail --silent --show-error --connect-timeout 3 --max-time 10 http://127.0.0.1:8091/health/worker || failed=1
  printf '\n'
  return "$failed"
}

cleanup_build_context() {
  [[ -n "$build_context" ]] || return 0
  if [[ "$build_context" != /tmp/plt-converter-build.* || ! -d "$build_context" ]]; then
    printf '拒绝清理异常构建目录：%s\n' "$build_context" >&2
    return 1
  fi
  rm -rf -- "$build_context"
  build_context=""
}

cleanup_candidate_images() {
  if [[ -n "$candidate_api_image" ]]; then
    docker image rm "$candidate_api_image" >/dev/null 2>&1 || true
  fi
  if [[ -n "$candidate_worker_image" ]]; then
    docker image rm "$candidate_worker_image" >/dev/null 2>&1 || true
  fi
  candidate_api_image=""
  candidate_worker_image=""
}

cleanup_before_replacement() {
  local exit_code="${1:-1}"
  trap - EXIT ERR INT TERM
  cleanup_candidate_images
  cleanup_build_context || true
  exit "$exit_code"
}

prepare_build_context() {
  build_context="$(mktemp -d /tmp/plt-converter-build.XXXXXX)"
  git archive --format=tar HEAD | tar -xf - -C "$build_context"
  export PLT_BUILD_CONTEXT="$build_context"
}

prepare_candidate_images() {
  local candidate_suffix="${1:0:12}-$$"
  candidate_api_image="plt-converter-api:candidate-$candidate_suffix"
  candidate_worker_image="plt-converter-worker:candidate-$candidate_suffix"
  export PLT_API_IMAGE="$candidate_api_image"
  export PLT_WORKER_IMAGE="$candidate_worker_image"
}

rollback() {
  local exit_code="${1:-1}"
  local recovery_failed=0
  local image_restore_failed=0
  trap - EXIT ERR INT TERM
  set +e

  printf '\n部署失败。\n' >&2
  if [[ "$rollback_enabled" == "1" && "$replacement_started" == "0" ]]; then
    printf '正在重新开放原转换 API，旧 Worker 保持运行……\n' >&2
    if ! "${compose[@]}" start api; then
      printf '原转换 API 启动失败。\n' >&2
      recovery_failed=1
    elif ! verify_runtime_health >&2; then
      printf '原转换 API 已启动，但健康检查未通过。\n' >&2
      recovery_failed=1
    else
      printf '原转换 API 已重新开放。\n' >&2
    fi
  elif [[ "$rollback_enabled" == "1" && -n "$old_api_image" && -n "$old_worker_image" ]]; then
    printf '正在恢复上一版转换服务……\n' >&2
    if ! docker image tag "$old_api_image" "$API_IMAGE"; then
      printf '上一版 API 镜像标签恢复失败。\n' >&2
      image_restore_failed=1
    fi
    if ! docker image tag "$old_worker_image" "$WORKER_IMAGE"; then
      printf '上一版 Worker 镜像标签恢复失败。\n' >&2
      image_restore_failed=1
    fi
    if [[ "$image_restore_failed" == "1" ]]; then
      recovery_failed=1
    elif ! "${compose[@]}" up -d --no-build --no-deps --force-recreate --wait --wait-timeout 120 api worker; then
      printf '上一版转换服务容器恢复失败。\n' >&2
      recovery_failed=1
    elif ! verify_runtime_health >&2; then
      printf '上一版转换服务已启动，但健康检查未通过。\n' >&2
      recovery_failed=1
    else
      printf '上一版转换服务容器已恢复。\n' >&2
    fi
  else
    printf '没有可自动恢复的上一版镜像，现有容器未被主动删除。\n' >&2
    recovery_failed=1
  fi

  if [[ "$recovery_failed" == "0" ]] && ! verify_backend >&2; then
    printf '转换服务已恢复，但主后台计费连接仍未通过。\n' >&2
    recovery_failed=1
  fi

  "${compose[@]}" ps >&2 || true
  cleanup_candidate_images
  cleanup_build_context || recovery_failed=1
  if [[ "$recovery_failed" == "1" ]]; then
    printf '自动恢复未完成，请立即人工检查转换服务。\n' >&2
  else
    printf '自动恢复完成。\n' >&2
  fi
  exit "$exit_code"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "缺少命令：$1"
}

check_env_variable() {
  local name="$1"
  awk -F= -v key="$name" '
    $1 == key {
      count++
      value = substr($0, index($0, "=") + 1)
      sub(/\r$/, "", value)
      if (length(value) > 0) nonempty++
    }
    END { exit !(count == 1 && nonempty == 1) }
  ' "$ENV_FILE" || fail "$ENV_FILE 中的 $name 必须有且只有一条非空配置"
}

check_port_owner() {
  local container_id project service
  while IFS= read -r container_id; do
    [[ -z "$container_id" ]] && continue
    project="$(docker inspect "$container_id" --format '{{index .Config.Labels "com.docker.compose.project"}}')"
    service="$(docker inspect "$container_id" --format '{{index .Config.Labels "com.docker.compose.service"}}')"
    if [[ "$project" != "$PROJECT_NAME" || "$service" != "api" ]]; then
      fail "8091 端口被非转换生产容器占用：$(docker inspect "$container_id" --format '{{.Name}}')"
    fi
  done < <(docker ps --filter publish=8091 -q)
}

check_redis() {
  local container_id health
  container_id="$("${compose[@]}" ps -q redis)"
  [[ -n "$container_id" ]] || fail "独立转换 Redis 未运行，请先检查生产服务"
  health="$(docker inspect "$container_id" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}')"
  [[ "$health" == "healthy" ]] || fail "独立转换 Redis 状态异常：$health"
}

wait_for_idle_queue() {
  local queue_json attempt
  for attempt in $(seq 1 60); do
    if queue_json="$("${compose[@]}" exec -T worker python -c '
import json
from app.job_queue import queue_load

queue = queue_load()
print(json.dumps(queue, ensure_ascii=False))
raise SystemExit(0 if queue["queued"] == 0 and queue["processing"] == 0 else 1)
')"; then
      printf '%s\n' "$queue_json"
      return
    fi
    if [[ "$attempt" == "1" ]]; then
      printf '转换队列仍有任务，最多等待 5 分钟……\n'
    fi
    sleep 5
  done
  fail "转换队列在 5 分钟内未清空，本次未部署"
}

verify_backend() {
  "${compose[@]}" exec -T api python -c '
import json
import os
import urllib.error
import urllib.request

url = os.environ["WX_BACKEND_URL"].rstrip("/") + "/api/v1/points/conversion/release"
request = urllib.request.Request(
    url,
    data=json.dumps({}).encode(),
    headers={
        "Content-Type": "application/json",
        "X-Conversion-Service-Token": os.environ["CONVERSION_SERVICE_TOKEN"],
    },
    method="POST",
)
try:
    status = urllib.request.urlopen(request, timeout=8).status
except urllib.error.HTTPError as error:
    status = error.code
if status != 400:
    raise SystemExit(f"主后台服务密钥验证失败，HTTP 状态码：{status}")
print("主后台网络、接口及服务密钥验证通过")
'
}

main() {
  require_command docker
  require_command git
  require_command curl
  require_command awk
  require_command flock
  require_command mktemp
  require_command rm
  require_command seq
  require_command tar

  exec 9>"/tmp/plt-converter-production-deploy.lock"
  flock -n 9 || fail "已有转换服务部署正在执行"

  cd "$PROJECT_DIR"
  [[ -f "$ENV_FILE" ]] || fail "缺少 $ENV_FILE"
  [[ -f "$COMPOSE_FILE" ]] || fail "缺少 $COMPOSE_FILE"
  [[ -f "$BUILD_COMPOSE_FILE" ]] || fail "缺少 $BUILD_COMPOSE_FILE"
  [[ "$(git branch --show-current)" == "main" ]] || fail "生产部署必须在 main 分支执行"
  [[ -z "$(git status --porcelain --untracked-files=all)" ]] || fail "存在未提交或未跟踪文件，请先处理"

  check_env_variable PLT_METRICS_TOKEN
  check_env_variable WX_BACKEND_URL
  check_env_variable CONVERSION_SERVICE_TOKEN

  log "拉取转换服务 main 分支"
  local old_head new_head remote_head reexec_count
  reexec_count="${PLT_DEPLOY_REEXEC_COUNT:-0}"
  [[ "$reexec_count" =~ ^[0-9]+$ ]] || fail "PLT_DEPLOY_REEXEC_COUNT 必须是非负整数"
  old_head="$(git rev-parse HEAD)"
  git pull --ff-only origin main
  new_head="$(git rev-parse HEAD)"
  remote_head="$(git rev-parse refs/remotes/origin/main)"
  [[ "$new_head" == "$remote_head" ]] || fail "本地 HEAD 与远端 main 不一致，拒绝部署"
  if [[ "$old_head" != "$new_head" ]]; then
    if ((reexec_count >= 1)); then
      fail "远端 main 在脚本重新加载后连续更新，请重新执行部署"
    fi
    log "部署脚本已更新，使用新版本继续"
    exec env PLT_DEPLOY_REEXEC_COUNT=$((reexec_count + 1)) "$SCRIPT_DIR/deploy-production.sh"
  fi

  check_env_variable PLT_METRICS_TOKEN
  check_env_variable WX_BACKEND_URL
  check_env_variable CONVERSION_SERVICE_TOKEN
  check_port_owner

  trap 'cleanup_before_replacement "$?"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  prepare_build_context
  prepare_candidate_images "$new_head"

  local services
  services="$("${build_compose[@]}" config --services | LC_ALL=C sort | tr '\n' ' ')"
  [[ "$services" == "api redis worker " ]] || fail "Compose 服务边界异常：$services"
  "${build_compose[@]}" config --quiet
  check_redis

  old_api_image="$(container_image_id api)"
  old_worker_image="$(container_image_id worker)"
  [[ -n "$old_api_image" && -n "$old_worker_image" ]] || fail "未找到当前生产 API/Worker；首次部署请按 README 执行"
  rollback_enabled=1

  log "构建转换服务镜像"
  "${build_compose[@]}" build api worker
  unset PLT_API_IMAGE PLT_WORKER_IMAGE

  trap 'rollback "$?"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM

  log "暂停转换 API 并等待旧 Worker 排空队列"
  "${compose[@]}" stop api
  wait_for_idle_queue

  log "更新转换服务容器"
  replacement_started=1
  docker image tag "$candidate_api_image" "$API_IMAGE"
  docker image tag "$candidate_worker_image" "$WORKER_IMAGE"
  "${compose[@]}" up -d --no-build --no-deps --wait --wait-timeout 120 api worker

  log "验证转换服务健康状态"
  verify_runtime_health

  log "验证主后台计费连接"
  verify_backend

  trap - EXIT INT TERM
  rollback_enabled=0

  log "部署成功：$(git rev-parse --short HEAD)"
  "${compose[@]}" ps || true
  cleanup_candidate_images
  cleanup_build_context
}

main "$@"

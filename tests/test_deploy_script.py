import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = PROJECT_ROOT / "scripts" / "deploy-production.sh"


FAKE_GIT = r"""#!/usr/bin/env bash
set -eu
printf 'git %s\n' "$*" >> "$DEPLOY_TEST_LOG"

current_head() {
  if [[ "${GIT_ADVANCE_TWICE:-0}" == "1" ]]; then
    cat "$GIT_HEAD_STATE_FILE"
  else
    printf '%s\n' "${GIT_LOCAL_HEAD:-same-head}"
  fi
}

case "${1:-}" in
  branch)
    printf 'main\n'
    ;;
  status)
    if [[ "${GIT_TRACKED_DIRTY:-0}" == "1" ]]; then
      printf ' M app/routes.py\n'
    fi
    if [[ "${GIT_TRACKED_STAGED:-0}" == "1" ]]; then
      printf 'A  app/new_module.py\n'
    fi
    if [[ "${GIT_UNTRACKED:-0}" == "1" && "$*" != *"--untracked-files=no"* ]]; then
      printf '?? app/local_override.py\n'
    fi
    ;;
  pull|fetch)
    if [[ "${GIT_ADVANCE_TWICE:-0}" == "1" ]]; then
      count="$(cat "$GIT_HEAD_STATE_FILE")"
      printf '%s\n' "$((count + 1))" > "$GIT_HEAD_STATE_FILE"
    fi
    ;;
  merge)
    ;;
  archive)
    /usr/bin/tar -cf - --files-from /dev/null
    ;;
  rev-parse)
    case "${2:-}" in
      HEAD)
        current_head
        ;;
      FETCH_HEAD|origin/main|refs/remotes/origin/main)
        if [[ "${GIT_ADVANCE_TWICE:-0}" == "1" ]]; then
          current_head
        else
          printf '%s\n' "${GIT_REMOTE_HEAD:-${GIT_LOCAL_HEAD:-same-head}}"
        fi
        ;;
      --short)
        current_head
        ;;
      *)
        current_head
        ;;
    esac
    ;;
esac
"""


FAKE_DOCKER = r"""#!/usr/bin/env bash
set -eu
printf 'docker %s\n' "$*" >> "$DEPLOY_TEST_LOG"
args="$*"

if [[ "${1:-}" == "compose" ]]; then
  case "$args" in
    *"config --services")
      printf 'api\nredis\nworker\n'
      ;;
    *"config --quiet")
      ;;
    *"ps -q redis")
      printf 'redis-id\n'
      ;;
    *"ps -q api")
      printf 'api-id\n'
      ;;
    *"ps -q worker")
      printf 'worker-id\n'
      ;;
    *"build api worker")
      printf 'build-context %s\n' "${PLT_BUILD_CONTEXT:-}" >> "$DEPLOY_TEST_LOG"
      printf 'build-images %s %s\n' "${PLT_API_IMAGE:-}" "${PLT_WORKER_IMAGE:-}" >> "$DEPLOY_TEST_LOG"
      if [[ "${BUILD_FAIL:-0}" == "1" ]]; then
        exit 1
      fi
      ;;
    *"stop api")
      ;;
    *"exec -T worker python -c"*)
      case "${QUEUE_MODE:-idle}" in
        idle)
          printf '{"queued": 0, "processing": 0}\n'
          ;;
        timeout)
          exit 1
          ;;
        term)
          kill -TERM "$PPID"
          exit 1
          ;;
        int)
          kill -INT "$PPID"
          exit 1
          ;;
      esac
      ;;
    *"exec -T api python -c"*)
      if [[ "${BACKEND_VERIFY_FAIL:-0}" == "1" ]]; then
        exit 1
      fi
      ;;
    *"start api")
      if [[ "${START_API_FAIL:-0}" == "1" ]]; then
        exit 1
      fi
      ;;
    *"up -d"*)
      if [[ "${DEPLOY_UP_MODE:-success}" == "always_fail" ]]; then
        exit 1
      fi
      ;;
    *" ps")
      ;;
    *)
      ;;
  esac
  exit 0
fi

case "${1:-}" in
  inspect)
    if [[ "$args" == *"{{.Image}}"* ]]; then
      case "${2:-}" in
        api-id) printf 'old-api-image\n' ;;
        worker-id) printf 'old-worker-image\n' ;;
      esac
    elif [[ "${2:-}" == "redis-id" ]]; then
      printf 'healthy\n'
    fi
    ;;
  ps)
    ;;
  image)
    ;;
esac
"""


FAKE_CURL = r"""#!/usr/bin/env bash
set -eu
printf 'curl %s\n' "$*" >> "$DEPLOY_TEST_LOG"
printf '{"status":"ok"}'
"""


class DeployScriptTest(unittest.TestCase):
    def run_deploy(self, **overrides):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            scripts_dir = root / "scripts"
            fake_bin = root / "fake-bin"
            scripts_dir.mkdir()
            fake_bin.mkdir()
            shutil.copy2(DEPLOY_SCRIPT, scripts_dir / "deploy-production.sh")
            (root / ".env.production").write_text(
                "PLT_METRICS_TOKEN=metrics\n"
                "WX_BACKEND_URL=https://backend.example\n"
                "CONVERSION_SERVICE_TOKEN=service-token\n",
                encoding="utf-8",
            )
            (root / "compose.production.yaml").write_text("services: {}\n", encoding="utf-8")
            (root / "compose.production.build.yaml").write_text(
                "services: {}\n", encoding="utf-8"
            )
            log_path = root / "commands.log"
            head_state_path = root / "git-head-state"
            head_state_path.write_text("0\n", encoding="utf-8")

            self.write_executable(fake_bin / "git", FAKE_GIT)
            self.write_executable(fake_bin / "docker", FAKE_DOCKER)
            self.write_executable(fake_bin / "curl", FAKE_CURL)
            self.write_executable(fake_bin / "flock", "#!/usr/bin/env bash\nexit 0\n")
            self.write_executable(fake_bin / "sleep", "#!/usr/bin/env bash\nexit 0\n")

            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                    "DEPLOY_TEST_LOG": str(log_path),
                    "GIT_HEAD_STATE_FILE": str(head_state_path),
                }
            )
            env.update({key: str(value) for key, value in overrides.items()})
            result = subprocess.run(
                ["bash", str(scripts_dir / "deploy-production.sh")],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            log = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
            return result, log

    @staticmethod
    def write_executable(path, content):
        path.write_text(textwrap.dedent(content), encoding="utf-8")
        path.chmod(0o755)

    def test_queue_timeout_reopens_stopped_api(self):
        result, log = self.run_deploy(QUEUE_MODE="timeout")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("start api", log)
        self.assertIn("原转换 API 已重新开放", result.stderr)

    def test_sigterm_reopens_stopped_api(self):
        result, log = self.run_deploy(QUEUE_MODE="term")

        self.assertEqual(result.returncode, 143)
        self.assertIn("start api", log)

    def test_sigint_reopens_stopped_api(self):
        result, log = self.run_deploy(QUEUE_MODE="int")

        self.assertEqual(result.returncode, 130)
        self.assertIn("start api", log)

    def test_failed_rollback_is_reported_as_incomplete(self):
        result, log = self.run_deploy(DEPLOY_UP_MODE="always_fail")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("image tag old-api-image plt-converter-api:production", log)
        self.assertIn("image tag old-worker-image plt-converter-worker:production", log)
        self.assertIn("自动恢复未完成", result.stderr)
        self.assertNotIn("上一版转换服务已恢复。", result.stderr)

    def test_local_head_must_equal_remote_main(self):
        result, log = self.run_deploy(GIT_LOCAL_HEAD="local-ahead", GIT_REMOTE_HEAD="remote-main")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("远端 main", result.stderr)
        self.assertNotIn("build api worker", log)

    def test_untracked_files_do_not_block_archived_build(self):
        result, log = self.run_deploy(GIT_UNTRACKED="1")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("git status --porcelain --untracked-files=no", log)
        self.assertIn("build api worker", log)

    def test_tracked_changes_are_rejected_before_build(self):
        result, log = self.run_deploy(GIT_TRACKED_DIRTY="1")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("已跟踪文件", result.stderr)
        self.assertNotIn("build api worker", log)

    def test_staged_tracked_files_are_rejected_before_build(self):
        result, log = self.run_deploy(GIT_TRACKED_STAGED="1")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("已跟踪文件", result.stderr)
        self.assertNotIn("build api worker", log)

    def test_docker_context_excludes_local_secrets_and_python_caches(self):
        dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")

        self.assertIn(".env*", dockerignore)
        self.assertIn("**/__pycache__/", dockerignore)
        self.assertIn("*.py[cod]", dockerignore)

    def test_health_checks_have_connection_and_total_timeouts(self):
        result, log = self.run_deploy()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        curl_calls = [line for line in log.splitlines() if line.startswith("curl ")]
        self.assertTrue(curl_calls)
        for call in curl_calls:
            self.assertIn("--connect-timeout", call)
            self.assertIn("--max-time", call)

    def test_backend_failure_is_rechecked_after_rollback(self):
        result, log = self.run_deploy(BACKEND_VERIFY_FAIL="1")

        self.assertNotEqual(result.returncode, 0)
        self.assertGreaterEqual(log.count("exec -T api python -c"), 2)
        self.assertIn("自动恢复未完成", result.stderr)
        self.assertNotIn("自动恢复完成。", result.stderr)

    def test_build_uses_an_archived_git_context(self):
        result, log = self.run_deploy()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("git archive --format=tar HEAD", log)
        context_lines = [line for line in log.splitlines() if line.startswith("build-context ")]
        self.assertEqual(len(context_lines), 1)
        build_context = Path(context_lines[0].removeprefix("build-context "))
        self.assertTrue(str(build_context).startswith("/tmp/plt-converter-build."))
        self.assertFalse(build_context.exists())

    def test_build_override_requires_an_explicit_clean_context(self):
        compose = (PROJECT_ROOT / "compose.production.yaml").read_text(encoding="utf-8")
        build_compose = (PROJECT_ROOT / "compose.production.build.yaml").read_text(
            encoding="utf-8"
        )
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertNotIn("build:", compose)
        self.assertIn("${PLT_BUILD_CONTEXT:?", build_compose)
        self.assertIn("git archive --format=tar HEAD", readme)

    def test_initial_deploy_enters_the_target_repository_before_git_commands(self):
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        initial_deploy = readme.split("首次部署完成后", 1)[0]

        subshell = initial_deploy.rsplit("(\n", 1)[1]
        self.assertLess(subshell.index("cd /opt/plt-converter"), subshell.index("git pull"))

    def test_build_failure_never_overwrites_production_image_tags(self):
        result, log = self.run_deploy(BUILD_FAIL="1")

        self.assertNotEqual(result.returncode, 0)
        build_image_lines = [line for line in log.splitlines() if line.startswith("build-images ")]
        self.assertEqual(len(build_image_lines), 1)
        self.assertIn("plt-converter-api:candidate-", build_image_lines[0])
        self.assertIn("plt-converter-worker:candidate-", build_image_lines[0])
        self.assertNotIn("image tag", log)

    def test_success_promotes_both_candidate_images(self):
        result, log = self.run_deploy()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(
            log,
            r"image tag plt-converter-api:candidate-[^ ]+ plt-converter-api:production",
        )
        self.assertRegex(
            log,
            r"image tag plt-converter-worker:candidate-[^ ]+ plt-converter-worker:production",
        )

    def test_second_main_update_stops_instead_of_using_stale_script(self):
        result, log = self.run_deploy(GIT_ADVANCE_TWICE="1")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("连续更新", result.stderr)
        self.assertNotIn("build api worker", log)

    def test_successful_deploy_still_completes(self):
        result, log = self.run_deploy()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("build api worker", log)
        self.assertIn("up -d", log)
        self.assertIn("/health/worker", log)


if __name__ == "__main__":
    unittest.main()

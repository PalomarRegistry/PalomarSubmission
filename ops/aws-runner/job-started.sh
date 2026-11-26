#!/usr/bin/env bash
# Job-started hook for Palomar's operator-launched AWS runner.
#
# The runner calls this (ACTIONS_RUNNER_HOOK_JOB_STARTED) after GitHub assigns
# it a job and before the job's first step. It lives on the instance, outside
# anything a pull request can edit, and admits only an operator dispatch of an
# approved workflow on main: a pull_request, pull_request_target, push,
# schedule or branch dispatch that names the runner's label is refused here
# even if it rewrote every workflow and the profile catalogue in its own tree.
#
# Refusing must not merely fail this step: the runner treats the hook as a step
# with an always() condition and still evaluates the job's later steps, so an
# `if: always()` step would run. A refusal therefore kills the runner's own
# processes before this script returns and powers the instance off, and no
# step of the refused job ever starts.
set -euo pipefail

repository=PalomarRegistry/PalomarSubmission
workflows="$repository/.github/workflows"
event="${GITHUB_EVENT_NAME:-}"
workflow_ref="${GITHUB_WORKFLOW_REF:-}"
job="${GITHUB_JOB:-}"

refuse() {
  echo "::error::Palomar AWS runner refused this job: $1 (event=$event repository=${GITHUB_REPOSITORY:-} workflow_ref=$workflow_ref job=$job)" >&2
  # Queue the poweroff with PID 1 first, so it survives this script being
  # killed with the runner's cgroup; then kill the runner so this never returns.
  sudo -n /usr/bin/systemctl --no-block poweroff || true
  sudo -n /usr/bin/pkill -KILL -f '/opt/actions-runner/bin/Runner\.(Worker|Listener)' || true
  sleep infinity
}

[[ "$event" == workflow_dispatch ]] || refuse "only workflow_dispatch may use this runner"
[[ "${GITHUB_REPOSITORY:-}" == "$repository" ]] || refuse "foreign repository"
[[ "${GITHUB_REF:-}" == refs/heads/main ]] || refuse "only the main branch may use this runner"
case "$workflow_ref:$job" in
  "$workflows/submission.yml@refs/heads/main:verify") ;;
  "$workflows/render-challenge.yml@refs/heads/main:render") ;;
  "$workflows/compatibility.yml@refs/heads/main:cold_build") ;;
  *) refuse "workflow and job are not approved for this runner" ;;
esac
echo "Palomar AWS runner accepted $workflow_ref job $job"

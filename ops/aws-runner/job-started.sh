#!/usr/bin/env bash
# Job-started hook for Palomar's operator-launched AWS runner.
#
# The runner calls this (ACTIONS_RUNNER_HOOK_JOB_STARTED) after GitHub assigns
# it a job and before the job's first step. A non-zero exit fails the job with
# no step run. It lives on the instance, outside anything a pull request can
# edit, and admits only an operator dispatch of an approved workflow on main:
# a pull_request, pull_request_target, push, schedule or a branch dispatch that
# names the runner's label is refused here even if it rewrote every workflow
# and the profile catalogue in its own tree.
set -euo pipefail

repository=PalomarRegistry/PalomarSubmission
workflows="$repository/.github/workflows"
event="${GITHUB_EVENT_NAME:-}"
workflow_ref="${GITHUB_WORKFLOW_REF:-}"
job="${GITHUB_JOB:-}"

refuse() {
  echo "::error::Palomar AWS runner refused this job: $1 (event=$event repository=${GITHUB_REPOSITORY:-} workflow_ref=$workflow_ref job=$job)" >&2
  exit 1
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

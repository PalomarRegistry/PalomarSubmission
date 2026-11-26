#!/usr/bin/env bash
# Operator tool for Palomar's operator-launched AWS verification runner.
#
#   launch-runner.sh setup               create the instance role, profile and security group
#   launch-runner.sh launch [options]    launch one ephemeral runner instance
#   launch-runner.sh status              list Palomar runner instances and registered runners
#   launch-runner.sh cleanup             delete offline runners that carry a Palomar AWS label
#
# launch options:
#   --profile-id ID        execution profile (default palomar-aws-r7i-16xlarge-v1); its id is the label
#   --instance-type TYPE   default r7i.16xlarge
#   --volume-gib N         gp3 root volume size (default 500)
#   --idle-minutes N       terminate if no job has started (default 180)
#   --job-minutes N        terminate a job that runs longer (default 1480: the 1,450-minute
#                          render timeout plus setup)
#   --max-lifetime-minutes N   absolute cap from boot (default 1680, 28 hours)
#   --dry-run              ask EC2 to authorize the launch without starting anything; no
#                          runner is registered
#
# AWS: profile `palomar` (account 945725587698), us-east-1. To launch in another
# account set AWS_PROFILE and PALOMAR_AWS_ACCOUNT (and AWS_REGION if needed), e.g.
#   AWS_PROFILE=riemann-hypothesis PALOMAR_AWS_ACCOUNT=419423654034 launch-runner.sh launch
# GitHub: the authenticated `gh` account must be able to administer PalomarRegistry/PalomarSubmission runners.
set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-palomar}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
EXPECTED_ACCOUNT="${PALOMAR_AWS_ACCOUNT:-945725587698}"
REPOSITORY=PalomarRegistry/PalomarSubmission
ROLE=palomar-runner
SECURITY_GROUP=palomar-runner-no-inbound
RUNNER_VERSION=2.338.2
RUNNER_SHA256=baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40
AMI_PARAMETER=/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
TAGS='{Key=project,Value=palomar}'

die() { echo "launch-runner: $*" >&2; exit 1; }

require_account() {
  local account
  account=$(aws sts get-caller-identity --query Account --output text)
  [[ "$account" == "$EXPECTED_ACCOUNT" ]] || die "AWS account is $account, expected $EXPECTED_ACCOUNT"
}

security_group_id() {
  aws ec2 describe-security-groups \
    --filters "Name=group-name,Values=$SECURITY_GROUP" "Name=tag:project,Values=palomar" \
    --query 'SecurityGroups[0].GroupId' --output text
}

setup() {
  require_account
  if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
    aws iam create-role --role-name "$ROLE" \
      --description "Palomar ephemeral runner: SSM Session Manager access only" \
      --tags Key=project,Value=palomar \
      --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
  fi
  # Nothing else: the instance terminates itself through its shutdown behaviour.
  # Each step is reconciled on every run, so an interrupted setup is repaired.
  aws iam attach-role-policy --role-name "$ROLE" \
    --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
  if ! aws iam get-instance-profile --instance-profile-name "$ROLE" >/dev/null 2>&1; then
    aws iam create-instance-profile --instance-profile-name "$ROLE" --tags Key=project,Value=palomar >/dev/null
  fi
  if [[ "$(aws iam get-instance-profile --instance-profile-name "$ROLE" --query 'InstanceProfile.Roles[].RoleName' --output text)" != "$ROLE" ]]; then
    aws iam add-role-to-instance-profile --instance-profile-name "$ROLE" --role-name "$ROLE"
    echo "instance profile updated: EC2 may refuse it for a minute while IAM propagates"
  fi
  [[ "$(aws iam list-attached-role-policies --role-name "$ROLE" --query 'AttachedPolicies[].PolicyName' --output text)" == AmazonSSMManagedInstanceCore ]] \
    || die "role $ROLE must have exactly AmazonSSMManagedInstanceCore attached"
  local group
  group=$(security_group_id)
  if [[ "$group" == None || -z "$group" ]]; then
    local vpc
    vpc=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
    group=$(aws ec2 create-security-group --group-name "$SECURITY_GROUP" --vpc-id "$vpc" \
      --description "Palomar ephemeral runner: no inbound rules" \
      --tag-specifications "ResourceType=security-group,Tags=[$TAGS]" --query GroupId --output text)
  fi
  local inbound
  inbound=$(aws ec2 describe-security-groups --group-ids "$group" --query 'length(SecurityGroups[0].IpPermissions)')
  [[ "$inbound" == 0 ]] || die "security group $group has inbound rules; remove them"
  echo "role/instance profile: $ROLE  security group: $group (no inbound rules)"
}

launch() {
  local profile_id=palomar-aws-r7i-16xlarge-v1 instance_type=r7i.16xlarge volume=500
  local idle=180 job=1480 lifetime=1680 dry_run=false
  while (($#)); do
    case "$1" in
      --profile-id) profile_id="$2"; shift 2 ;;
      --instance-type) instance_type="$2"; shift 2 ;;
      --volume-gib) volume="$2"; shift 2 ;;
      --idle-minutes) idle="$2"; shift 2 ;;
      --job-minutes) job="$2"; shift 2 ;;
      --max-lifetime-minutes) lifetime="$2"; shift 2 ;;
      --dry-run) dry_run=true; shift ;;
      *) die "unknown launch option $1" ;;
    esac
  done
  [[ "$profile_id" =~ ^palomar-aws-[a-z0-9-]+-v[0-9]+$ ]] || die "not an AWS profile id: $profile_id"
  for value in "$volume" "$idle" "$job" "$lifetime"; do
    [[ "$value" =~ ^[1-9][0-9]*$ ]] || die "numeric options must be positive integers"
  done
  require_account
  local group ami
  group=$(security_group_id)
  [[ "$group" != None && -n "$group" ]] || die "run '$0 setup' first"
  ami=$(aws ssm get-parameter --name "$AMI_PARAMETER" --query Parameter.Value --output text)
  if [[ "$dry_run" == false ]]; then
    local running
    running=$(aws ec2 describe-instances \
      --filters "Name=tag:palomar-runner-label,Values=$profile_id" \
                "Name=instance-state-name,Values=pending,running" \
      --query 'length(Reservations[].Instances[])')
    [[ "$running" == 0 ]] || die "an instance for $profile_id is already pending or running"
  fi

  # A just-in-time configuration registers exactly one ephemeral runner with
  # exactly this label. Unlike a registration token it cannot enrol further
  # runners, so a copy read off the instance is worth nothing once this runner
  # has connected. An unused registration is removed by `cleanup`.
  local jit=dry-run-placeholder
  local name
  name="$profile_id-$(date -u +%Y%m%dT%H%M%SZ)"
  if [[ "$dry_run" == false ]]; then
    jit=$(gh api -X POST "repos/$REPOSITORY/actions/runners/generate-jitconfig" \
      -f name="$name" -F runner_group_id=1 -f "labels[]=$profile_id" -f work_folder=_work \
      --jq .encoded_jit_config)
  fi
  local user_data
  user_data=$(mktemp)
  trap 'rm -f "$user_data"' RETURN
  {
    echo '#!/bin/bash'
    printf 'REPOSITORY=%q\n' "$REPOSITORY"
    printf 'RUNNER_LABEL=%q\n' "$profile_id"
    printf 'RUNNER_NAME=%q\n' "$name"
    printf 'JIT_CONFIG=%q\n' "$jit"
    printf 'RUNNER_VERSION=%q\nRUNNER_SHA256=%q\n' "$RUNNER_VERSION" "$RUNNER_SHA256"
    printf 'IDLE_MINUTES=%q\nJOB_MINUTES=%q\nMAX_LIFETIME_MINUTES=%q\n' "$idle" "$job" "$lifetime"
    printf 'JOB_STARTED_HOOK_B64=%q\n' "$(base64 -w0 "$HERE/job-started.sh")"
    cat "$HERE/user-data.sh"
  } > "$user_data"

  # shellcheck disable=SC2054 # the commas are inside AWS CLI shorthand values
  local arguments=(
    --image-id "$ami" --instance-type "$instance_type" --count 1
    --iam-instance-profile "Name=$ROLE" --security-group-ids "$group"
    --instance-initiated-shutdown-behavior terminate
    --metadata-options HttpTokens=required,HttpPutResponseHopLimit=1,HttpEndpoint=enabled,InstanceMetadataTags=disabled
    --block-device-mappings "DeviceName=/dev/sda1,Ebs={VolumeSize=$volume,VolumeType=gp3,Iops=6000,Throughput=500,DeleteOnTermination=true,Encrypted=true}"
    --user-data "file://$user_data"
    --tag-specifications
      "ResourceType=instance,Tags=[$TAGS,{Key=Name,Value=$profile_id},{Key=palomar-runner-label,Value=$profile_id}]"
      "ResourceType=volume,Tags=[$TAGS,{Key=palomar-runner-label,Value=$profile_id}]"
      "ResourceType=network-interface,Tags=[$TAGS]"
  )
  if [[ "$dry_run" == true ]]; then
    local output
    if output=$(aws ec2 run-instances --dry-run "${arguments[@]}" 2>&1); then
      die "unexpected success from a dry run"
    fi
    if [[ "$output" == *DryRunOperation* ]]; then
      echo "dry run authorized: $instance_type, $ami, ${volume} GiB gp3, label $profile_id"
      echo "user-data size: $(wc -c < "$user_data") bytes (limit 16384)"
      return 0
    fi
    die "dry run refused: $output"
  fi
  local instance
  instance=$(aws ec2 run-instances "${arguments[@]}" --query 'Instances[0].InstanceId' --output text)
  echo "launched $instance ($instance_type) for $profile_id"
  echo "watch registration: gh api repos/$REPOSITORY/actions/runners --jq '.runners[] | select(.name|startswith(\"$profile_id\")) | {name,status,busy}'"
  echo "shell (no inbound ports): aws ssm start-session --target $instance"
  echo "setup log on the instance: /var/log/palomar-runner-setup.log"
}

status() {
  require_account
  # shellcheck disable=SC2016 # backticks are JMESPath literals
  aws ec2 describe-instances --filters "Name=tag:project,Values=palomar" \
    "Name=instance-state-name,Values=pending,running,stopping,shutting-down" \
    --query 'Reservations[].Instances[].[InstanceId,InstanceType,State.Name,LaunchTime,Tags[?Key==`palomar-runner-label`]|[0].Value]' \
    --output table
  gh api "repos/$REPOSITORY/actions/runners" \
    --jq '.runners[] | select(.name | startswith("palomar-aws-")) | {id,name,status,busy}'
}

cleanup() {
  local ids
  ids=$(gh api "repos/$REPOSITORY/actions/runners" \
    --jq '.runners[] | select((.name | startswith("palomar-aws-")) and .status == "offline") | .id')
  for id in $ids; do
    # A runner killed mid-job stays "busy" until GitHub notices it is gone.
    if gh api -X DELETE "repos/$REPOSITORY/actions/runners/$id" >/dev/null 2>&1; then
      echo "deleted offline runner $id"
    else
      echo "runner $id is still marked busy; rerun cleanup later"
    fi
  done
}

case "${1:-}" in
  setup) setup ;;
  launch) shift; launch "$@" ;;
  status) status ;;
  cleanup) cleanup ;;
  *) sed -n '2,25p' "$0" >&2; exit 2 ;;
esac

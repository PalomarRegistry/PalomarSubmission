# shellcheck shell=bash
# Body of the EC2 user-data for Palomar's operator-launched runner.
#
# launch-runner.sh prepends a shebang and these variables, then this file:
#   REPOSITORY RUNNER_LABEL RUNNER_NAME JIT_CONFIG RUNNER_VERSION RUNNER_SHA256
#   IDLE_MINUTES JOB_MINUTES MAX_LIFETIME_MINUTES JOB_STARTED_HOOK_B64
#
# It runs once, as root, at first boot. Every failure path powers the
# instance off, and the instance is launched with
# instance-initiated-shutdown-behavior=terminate, so poweroff is termination.
set -euo pipefail
exec > >(tee -a /var/log/palomar-runner-setup.log) 2>&1
trap 'echo "palomar-runner: setup failed at line $LINENO; terminating"; systemctl --no-block poweroff' ERR

# The watchdog first, so nothing below can leave an instance running forever.
# The first-boot time is recorded once; a reboot, instance recovery or watchdog
# restart finds it and powers off at once rather than granting a new allowance
# or restarting the runner without the metadata firewall below.
install -d -m 0755 /opt/palomar-runner /var/lib/palomar-runner
date +%s > /var/lib/palomar-runner/first-boot
cat > /opt/palomar-runner/watchdog.sh <<WATCHDOG
#!/usr/bin/env bash
# Terminate when no job arrived in time, when the job overran, or at the cap.
set -u
booted=\$(cat /var/lib/palomar-runner/first-boot)
if [[ "\$(cat /proc/sys/kernel/random/boot_id)" != "\$(cat /var/lib/palomar-runner/boot-id)" ]]; then
  echo "palomar-runner: not the first boot; terminating"; systemctl --no-block poweroff; exit 0
fi
job_started=\$(cat /var/lib/palomar-runner/job-started 2>/dev/null || true)
while sleep 30; do
  now=\$(date +%s)
  if [[ -z "\$job_started" ]] && pgrep -u runner -f Runner.Worker >/dev/null; then
    job_started=\$now
    echo "\$now" > /var/lib/palomar-runner/job-started
    echo "palomar-runner: job started"
  fi
  if [[ -z "\$job_started" ]] && (( now - booted > ${IDLE_MINUTES} * 60 )); then
    echo "palomar-runner: no job within ${IDLE_MINUTES} minutes; terminating"; break
  fi
  if [[ -f /var/lib/palomar-runner/runner-started ]] && ! systemctl is-active --quiet palomar-runner.service; then
    echo "palomar-runner: the runner has stopped; terminating"; break
  fi
  if [[ -n "\$job_started" ]] && (( now - job_started > ${JOB_MINUTES} * 60 )); then
    echo "palomar-runner: job exceeded ${JOB_MINUTES} minutes; terminating"; break
  fi
  if (( now - booted > ${MAX_LIFETIME_MINUTES} * 60 )); then
    echo "palomar-runner: reached the ${MAX_LIFETIME_MINUTES}-minute lifetime cap; terminating"; break
  fi
done
systemctl --no-block poweroff
WATCHDOG
chmod 0755 /opt/palomar-runner/watchdog.sh
cat /proc/sys/kernel/random/boot_id > /var/lib/palomar-runner/boot-id
cat > /etc/systemd/system/palomar-watchdog.service <<'UNIT'
[Unit]
Description=Palomar runner lifetime watchdog

[Service]
ExecStart=/opt/palomar-runner/watchdog.sh
Restart=always

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now palomar-watchdog.service

# Read what is needed from instance metadata, then close it to the job user.
imds_token=$(curl -fsS -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 300' http://169.254.169.254/latest/api/token)
instance_id=$(curl -fsS -H "X-aws-ec2-metadata-token: $imds_token" http://169.254.169.254/latest/meta-data/instance-id)

# Packages the workflows expect from a GitHub- or Namespace-hosted Ubuntu 24.04
# image: a compiler for bubblewrap and native gems, Git, curl and archive tools
# for elan, Lake and the Mathlib cache, and the libraries the prebuilt Ruby that
# ruby/setup-ruby installs links against.
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  build-essential ca-certificates curl git jq unzip zip zstd xz-utils file \
  pkg-config meson ninja-build libcap-dev \
  libyaml-0-2 libyaml-dev libssl-dev libffi-dev zlib1g-dev libgmp-dev \
  python3 python3-venv nftables apparmor dbus-user-session >/dev/null

# The job user. Passwordless sudo matches the hosted images: the workflow uses
# it for apt-get and to load bubblewrap's AppArmor profile. Candidate code
# never runs as this user outside bubblewrap.
useradd --create-home --shell /bin/bash runner
echo 'runner ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/90-runner
chmod 0440 /etc/sudoers.d/90-runner
runner_uid=$(id -u runner)
# A user manager for the job user. The verifier asks it for a Delegate=yes
# scope (systemd-run --user --scope), the systemd-host path that ci.yml's
# hosted `test` job proves; on Namespace a privileged container lets
# scripts/cgroup_delegate.py build the same delegated subtree by hand.
loginctl enable-linger runner
for _ in $(seq 60); do [[ -S "/run/user/$runner_uid/bus" ]] && break; sleep 1; done
[[ -S "/run/user/$runner_uid/bus" ]]

# Instance metadata (this user-data, the instance role's credentials) is
# unreachable from the job user's own sockets. Root services such as
# the SSM agent keep it.
nft -f - <<NFT
table inet palomar {
  chain output {
    type filter hook output priority 0; policy accept;
    ip daddr 169.254.169.254 meta skuid $runner_uid reject
    ip6 daddr fd00:ec2::254 meta skuid $runner_uid reject
  }
}
NFT

# setup-ruby's prebuilt Rubies are only relocatable to the hosted tool cache path.
install -d -o runner -g runner /opt/hostedtoolcache

# The pinned runner, verified before it is unpacked.
install -d -o runner -g runner /opt/actions-runner
curl -fsSL --proto '=https' --tlsv1.2 -o /tmp/actions-runner.tar.gz \
  "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
echo "${RUNNER_SHA256}  /tmp/actions-runner.tar.gz" | sha256sum --check --quiet
tar -xzf /tmp/actions-runner.tar.gz -C /opt/actions-runner
rm /tmp/actions-runner.tar.gz
/opt/actions-runner/bin/installdependencies.sh >/dev/null
chown -R runner:runner /opt/actions-runner

echo "$JOB_STARTED_HOOK_B64" | base64 -d > /opt/palomar-runner/job-started.sh
chmod 0755 /opt/palomar-runner/job-started.sh
# Root-owned and outside the job's writable tree.
cat > /opt/actions-runner/.env <<ENV
ACTIONS_RUNNER_HOOK_JOB_STARTED=/opt/palomar-runner/job-started.sh
RUNNER_TOOL_CACHE=/opt/hostedtoolcache
AGENT_TOOLSDIRECTORY=/opt/hostedtoolcache
ImageOS=ubuntu24
XDG_RUNTIME_DIR=/run/user/$runner_uid
DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$runner_uid/bus
LANG=C.UTF-8
ENV
chown root:root /opt/actions-runner/.env
chmod 0644 /opt/actions-runner/.env

# The just-in-time configuration registers this one ephemeral runner: one job,
# then GitHub removes it. systemd reads it as root, before dropping to the job
# user, and the runner masks it.
install -d -m 0700 /etc/palomar-runner
umask 077
printf 'ACTIONS_RUNNER_INPUT_JITCONFIG=%s\n' "$JIT_CONFIG" > /etc/palomar-runner/jit.env
umask 022
unset JIT_CONFIG

cat > /etc/systemd/system/palomar-runner.service <<UNIT
[Unit]
Description=Palomar ephemeral GitHub Actions runner
After=network-online.target user@$runner_uid.service
Wants=network-online.target

[Service]
User=runner
WorkingDirectory=/opt/actions-runner
Environment=XDG_RUNTIME_DIR=/run/user/$runner_uid
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$runner_uid/bus
# Optional: it is deleted once the runner starts, and stop-post must still run.
EnvironmentFile=-/etc/palomar-runner/jit.env
ExecStart=/opt/actions-runner/run.sh
# The job is over (or the runner could not start): terminate the instance.
ExecStopPost=+/usr/bin/systemctl --no-block poweroff
Restart=no
KillMode=control-group
TimeoutStopSec=60

UNIT
systemctl daemon-reload
# Started, never enabled: it must not come back on a later boot.
systemctl start palomar-runner.service
touch /var/lib/palomar-runner/runner-started
rm -f /etc/palomar-runner/jit.env
# cloud-init's on-disk copies of this script carry the configuration too.
rm -f /var/lib/cloud/instance/user-data.txt /var/lib/cloud/instance/user-data.txt.i \
  /var/lib/cloud/instance/scripts/part-001
trap - ERR
echo "palomar-runner: started $RUNNER_NAME on $instance_id"

# Operator-launched AWS runner

This directory launches the runner for the operator-only
`palomar-aws-r7i-16xlarge-v1` execution profile: one EC2 instance, for one
job, in Palomar's AWS account (`palomar`, 945725587698, us-east-1). Use it
only when a submission has already failed on `palomar-namespace-32x64-v1`, or
when its measured resource use rules out both Namespace tiers.
[`SECURITY.md`](../../SECURITY.md) ("The operator-launched AWS runner") states
the containment and trigger model; this file is the procedure.

- `launch-runner.sh` creates the role and security group (`setup`), launches an
  instance (`launch`), lists instances and registered runners (`status`) and
  deletes offline runner registrations (`cleanup`).
- `user-data.sh` is the first-boot script. It installs the packages the
  workflows expect, a `runner` user with a lingering systemd user manager, the
  pinned Actions runner registered `--ephemeral --no-default-labels` with the
  profile id as its only label, and a watchdog.
- `job-started.sh` is the runner's job-started hook. It fails every job that is
  not a `workflow_dispatch` on `main` of `submission.yml` (`verify`),
  `render-challenge.yml` (`render`) or `compatibility.yml` (`cold_build`).

## Machine

r7i.16xlarge: 64 vCPU and 512 GiB, so the profile requires 480 GiB of visible
memory (the same 93.75% of nominal that `palomar-namespace-32x64-v1` requires)
and 64 effective CPUs. One 5,141-module replay of the motivating project took
88 minutes and 20.2 GB in the checker alone, so the whole project's build,
two exports and three kernel replays plausibly need well over the 64 GiB and
five and a half hours a Namespace job has. 512 GiB keeps the 95%/98% cgroup
thresholds far from any plausible peak. r7i.8xlarge (32 vCPU / 256 GiB, half
the price) needs its own profile and is the fallback only if a measured peak
fits comfortably in 240 GiB.

The root volume is 500 GiB gp3 at 6,000 IOPS and 500 MB/s, encrypted and
deleted on termination. Peak use is estimated at 150 GiB: the Mathlib cache and
toolchains (about 10 GiB), the project's own build output (source is capped at
500 MiB; built `.olean` files run tens of times larger), two whole-environment
exports and the verifier's snapshots of them, and the profile's 20 GiB free
workspace floor. 500 GiB leaves more than three times that, for about USD 2 a
day including the provisioned IOPS and throughput.

The job allows 1,440 minutes (24 hours) and the verifier 82,800 seconds (23
hours), leaving an hour for setup, finalization and report upload. That is
roughly double a generous estimate for this project, stays inside the 24-hour
lifetime of a job's `GITHUB_TOKEN` and far inside GitHub's five-day limit for
self-hosted jobs, and bounds the cost of a run that never finishes.

## Cost

On-demand r7i.16xlarge in us-east-1 is USD 4.2336 an hour. A 12-hour
verification costs about USD 53 with storage; the watchdog's 28-hour cap
bounds a single instance at about USD 120. Rendering is a second launch of the
same kind, usually much shorter.

## Procedure

Prerequisites: the `palomar` AWS CLI profile, an EC2 On-Demand Standard vCPU
quota of at least 64 in us-east-1, and a `gh` login that can administer this
repository's runners. Check the quota with

```bash
aws service-quotas get-service-quota --profile palomar --region us-east-1 \
  --service-code ec2 --quota-code L-1216C47A --query Quota.Value
```

1. Once per account: `ops/aws-runner/launch-runner.sh setup`.
2. Check the launch is authorized without starting anything:
   `ops/aws-runner/launch-runner.sh launch --dry-run`.
3. Launch: `ops/aws-runner/launch-runner.sh launch`. Registration takes about
   three minutes; `launch-runner.sh status` shows the runner `online`. With no
   job within 180 minutes the instance terminates.
4. Queue the verification from a PalomarServer checkout, as a Technical
   Maintainer (dry run first, then with `--apply`):

   ```bash
   node tools/retry-verification.mjs --id <submission-id> \
     --profile palomar-aws-r7i-16xlarge-v1 --reason "<why>"
   node tools/retry-verification.mjs --id <submission-id> \
     --profile palomar-aws-r7i-16xlarge-v1 --reason "<why>" --apply
   ```

   The server's dispatcher sends the `workflow_dispatch`; the `verify` job runs
   on the instance, which terminates when it ends.
5. After acceptance, Reviewer dispatches `render-challenge.yml` with the same
   profile. Its `render` job waits for a runner: launch a second instance
   (step 3) when it is queued.
6. `launch-runner.sh status` should show no instances; `launch-runner.sh
   cleanup` removes any registration an instance left behind.

`aws ssm start-session --profile palomar --region us-east-1 --target <instance>`
opens a shell; `/var/log/palomar-runner-setup.log` is the first-boot log.

## Maintenance

The runner is pinned (`RUNNER_VERSION` and `RUNNER_SHA256` in
`launch-runner.sh`) and registered with `--disableupdate`. GitHub stops
assigning jobs to a runner more than 30 days behind the latest release, so bump
the pin, with the checksum from the release notes, when a launch registers but
never receives its job.

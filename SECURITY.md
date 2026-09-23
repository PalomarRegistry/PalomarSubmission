# Palomar security policy

Palomar mechanically checks Lean projects dispatched here by the submission
server at <https://submit.palomar-registry.org>. A submission is untrusted
input, not merely untrusted Lean source. This document describes the threat
model, the verification boundary, and how to report a problem without exposing
other users to it.

## Threat model

The submitter controls the dispatched request fields and every byte of the
referenced Git commit. In particular, the following are treated as potentially
hostile:

- `Challenge.lean`, `Solution.lean`, and any other source file;
- `formalization.yaml`, including parser-expansion and malformed-structure attacks;
- `lakefile.toml`, `lake-manifest.json`, and dependency Lake files;
- dependency repositories and all Git objects fetched for them;
- Lake elaborator-time IO, build scripts, compiler subprocesses, and generated
  executables; and
- committed `.lake` directories, `.olean` files, traces, and other build
  artifacts.

The attacker may try to change the meaning of the challenge, obtain credentials,
modify the toolchain or another verifier tool, overwrite the mechanical report,
write outside the build tree, use the network, inject shell syntax, or consume
excessive resources. A project need not contain an obviously malicious Lean
declaration to be dangerous: loading a dependency's Lake configuration can run
elaborator IO before a normal `lake env` or `lake build` command begins.

The integrity assets are the statement being compared, the provenance assigned
to its imports, the verifier binaries and scripts, and the final report. The
confidentiality boundary includes any credentials held by the GitHub Actions
environment.

## Defensive boundary

The shape of the pipeline is short enough to check against the file.
[`.github/workflows/submission.yml`](.github/workflows/submission.yml) has two
explicit triggers, `workflow_dispatch` and `workflow_call`. The server dispatches
authoritative registry runs. This file has no direct push, comment, or pull-request
trigger; reusable callers can invoke a mechanical preflight from their own CI.
A public repository may call the same job as a predictive mechanical preflight,
but that caller has no Palomar state or credential and cannot register its
result. There are two jobs: `profile` resolves approved runner labels, and `verify`, and its `permissions` block is
`contents: read`. Its inputs are a repository, a commit, a pinned pipeline
commit for reusable calls, an opaque submission id, a closed
`preflight`/`full`/`correction` mode, and a JSON object whose keys are checked against the fixed `OPTIONAL_FIELDS`
allowlist in [`scripts/submission_contract.py`](scripts/submission_contract.py):
three optional paths, an existing Palomar id, and the declared authorization
relationship with its optional evidence. That evidence is submitter-written
prose and is meant to be read: everything dispatched here is visible on the run
page of a public repository, which is why the allowlist is short. The
submitter's private notes from the submission form are not among these fields,
and keeping them out is the submission server's doing rather than this
workflow's. What the allowlist adds is that a caller sending them anyway fails
the contract check instead of being quietly accepted. There is no
input for the submitter's GitHub identity, and none for an editorial review,
which happens elsewhere and afterwards. The run uploads exactly one mode-specific artifact,
`preflight-report-<request_id>` or `mechanical-report-<request_id>`, holding the bounded
`mechanical-report.json`. Both modes execute the same intake preparation; only `full`
may continue into candidate-controlled verification.
If the file and this paragraph disagree, the file is right.

The submission server emits `technical-test` only after browser sign-in has
established active Technical Maintainer membership. This verifier treats it like
every dispatched field: recorded, not trusted. The report preserves the value
rather than presenting it as author or maintainer approval. A submission carrying
it cannot be registered; the submission server and reviewer enforce that rule,
not this credential-free verification workflow.

### Intake and dependency provenance

- The verifier accepts only a public, credential-free
  `https://github.com/owner/repository` URL and a full 40-character commit SHA.
  Dynamic request values are read from the workflow event payload and passed as
  files or subprocess arguments, not interpolated into shell programs.
- [`scripts/submission_contract.py`](scripts/submission_contract.py) caps
  `formalization.yaml` at 256 KiB and parses it in the credential-free intake
  job with PyYAML's safe loader, duplicate-key rejection, and explicit
  rejection of YAML merge keys before they can be expanded. The same module
  owns the closed dispatch envelope and provenance contract; the verifier
  orchestrator applies the commit, existing-id, and authorization field rules
  and consumes the metadata without a fallback parser.
- Git dependencies are materialized directly at the full commits recorded in
  the submitted manifest. Git hooks, global/system Git configuration, local
  transport, and interactive credential prompts are disabled. The verifier
  does not run `lake update` or dependency post-update hooks.
- An allowlisted Challenge root must belong to its canonical branch history or,
  only where its trusted-root configuration opts in, be named exactly by a
  canonical semantic-version release tag. The fallback queries the canonical
  remote after branch ancestry fails; submitted and non-release tags do not
  expand the accepted history.
- Checkout containment uses the verifier-owned clone path supplied by the
  caller, never ancestor Git metadata or submitted source. The renderer
  revalidates every nested project-path component when preparing its workspace.
  It retains the accepted Challenge, Solution, and Comparator configuration at
  their original paths, binds those paths to the configured dotted modules,
  and replaces only the project Lakefile and manifest. Writable directories
  are checked against the explicitly supplied workspace boundary.
- The post-acceptance renderer requires the complete six-field path set from
  registration and records it in render report schema 2. An empty project path
  means the repository root; none of the five accepted file paths may be empty.
  Preparation and execution reject old report shapes instead of guessing
  conventional filenames. Lake and Verso compile the configured Challenge
  module under its original identity; sanitization alone maps that module's
  generated page to the stable public `Challenge/index.html` artifact path.
- Every submitted, dependency, and separately recorded substantive source must
  be a public GitHub repository pinned to a full commit so registration can
  preserve the complete accepted source graph in native GitHub forks. Git LFS
  pointers are rejected throughout that graph. Submitted and substantive
  repositories containing submodules are rejected; an inert dependency
  submodule gitlink is allowed only because the verifier never initializes or
  reads it and the native fork preserves the exact ordinary Git object.
- An allowlisted Mathlib, Tau Ceti, or CSLib revision is trusted only if Git
  proves that it is an ancestor of the configured branch in the canonical repository.
  A compatibility exception may name an exact historical commit explicitly;
  it does not make adjacent history trusted. Known repository moves are handled
  as explicit aliases, not as arbitrary URL equivalence.
- The allowlisted root's own `lake-manifest.json` is authoritative for its
  pinned dependency closure. Every package name, canonical repository, and
  revision in that closure must match the submission's flattened manifest.
  Reusing a trusted package name while substituting a fork or commit is rejected.
- Each canonical allowlisted repository has exactly one package role in the
  flattened manifest. A repository alias cannot introduce a second trusted
  name or a second candidate-writable build directory.
- The transitive source closure of `Challenge.lean` may contain only Lean core
  or the verified pinned closure of an allowlisted root. Candidate-local helper
  imports remain rejected, and so does every source from a project Palomar has
  already accepted: an earlier record confers no import privilege. The closure
  comes from Lean's own `--src-deps` result, and
  every dependency source attributed to a Git package must be a byte-for-byte
  match for a file tracked at that package's pinned commit. Sources below a
  sandbox-writable directory are never trusted.

### Sandbox confinement

Before Lake is started, the verifier deletes all submitted `.lake` state from
the root project and materialized packages. It creates fresh `.lake/build` and
`.lake/config` directories; these are the only project directories ever made
writable and executable inside the outer sandbox. A suffix scan rejects common
committed Lean, trace, native, and shared-library artifacts outside that fresh
state. That scan is an early compatibility rejection, not the proof of module
integrity: candidate configuration could copy arbitrarily named bytes into a
fresh build directory.

Statement integrity instead comes from a separate build path. Mathlib's
trusted cache is run with a disposable copy of official Mathlib as the
workspace root and with symlinks to the exact independently verified official
closure. Other allowlisted roots are built from their own pinned configuration.
Candidate Lake configuration never runs during these operations. Immediately before the
network-enabled Mathlib cache command, the verifier deletes and recreates every
canonical `.lake` tree in Mathlib's closure, then hard-links the verified,
read-only sources into a disposable sibling workspace. The network-enabled
command can write complete `.lake` roots only there, allowing ordinary Lake
release facets without granting write access to canonical source or build
state. Before promotion, the verifier rechecks package and dependency-link
mappings, rejects special files, external hard links, and escaping build
symlinks, and accepts non-build state only as generic regular
artifact/`.trace` pairs. It validates every package before atomically moving
those build trees and pairs into the fresh canonical `.lake` roots. The later
network-disabled replay can write only canonical `build` and `config`
directories and one pre-existing exact ProofWidgets replay marker. A second
network-disabled replay from a verifier-authored empty root creates the
dependency-scoped Lake configuration that submitted configuration later reads;
it has the same canonical write boundary. Trusted-root Lake URL resolution is
pinned to that root's verified manifest and is accepted only
when the flattened submission manifest names the same canonical GitHub
repository. Immediately before each qualified-root build, the verifier likewise
deletes and recreates every `.lake` tree owned by that root. Mathlib-owned cache
output and the verifier-created flattened closure links remain read-only. The
root build can write only the freshly recreated `build` and `config`
directories of every package it owns, and runs with network disabled. Trusted
build directories are then frozen read/execute-only. The verifier compiles `Challenge.lean`
directly with trusted Lean against only the frozen allowlisted dependencies, outside the
candidate's Lake plan, records the resulting `Challenge.olean` digest, and
copies only its exact module artifact set into a fresh protected directory under
an unpredictable per-run top-level alias. The `LEAN_PATH` under which the
Challenge is exported resolves that directory, Lean core, and every frozen
trusted build directory before all candidate build paths. Candidate Lake
configuration can still build arbitrary proof dependencies in its own fresh
directories, but it cannot replace the statement module or a trusted dependency
used to compile it. A nonstandard output layout fails closed.

Every invocation that can load project Lake configuration runs under the same
outer bubblewrap policy. This includes Mathlib cache retrieval, `lake env` used
to discover Lean paths, the Solution build and both exports. The sandbox root is an empty
tmpfs: there is no blanket read rule for the runner filesystem, and a path that
is not bound in does not exist. The policy binds read-only the submitted source
tree, a small explicit set of certificate/name-service files (and the
`/etc/alternatives` symlink farm Debian tools resolve through), the selected
Lean toolchain, pinned verifier programs, and immutable system/Python runtime
directories, and binds writable only the fresh build and Lake-configuration
directories. Unrelated runner temporary directories, the host home directory,
the report, `/run` and `/var` (and so every host daemon socket), and sibling
process state (a private `/proc`) are absent from the sandbox's mount
namespace. Its PID, IPC, UTS, cgroup and user namespaces are unshared and
nested user namespaces are disabled, so a candidate cannot build a second
sandbox to escape the first. Sandboxed Git ignores system and global
configuration and cannot prompt for credentials, preventing ambient runner
configuration from rewriting authenticated remotes.

Normal configuration and comparison run with the network namespace unshared,
so there is no interface to reach rather than a filter to pass. The verified
Mathlib cache client has a narrowly scoped exception because
downloading official cache artifacts is its purpose. During that phase Mathlib
is the workspace root and its exact official closure is exposed through
temporary package links. The links are deleted immediately afterward, the
trusted cache output is frozen, and candidate Lake configuration is not loaded
while network access is available.

The judge is the submitted toolchain's own `lake comparator`, and it never
builds anything. Palomar builds the Solution under the candidate policy,
exports the canonical Challenge and the built Solution with the toolchain's
`leanexport` (each export written by the babysitter to a verifier-owned file
that no candidate phase can write to, then snapshotted), and runs
`lake comparator --challenge-from-export --solution-from-export` over the two
files. That form skips the comparator's own dependency resolution, which would
otherwise evaluate the candidate's Lake configuration with the network enabled,
and it means the comparator never checks that the exports match the project:
Palomar produced both, from the canonical Challenge and the verified checkout,
and that is where the trust sits. The judge phase runs under a policy of its
own that binds no candidate tree at all: the two exports, the protected
configuration, the toolchain, the system directories and a fresh scratch
directory. It is the one phase that keeps user namespaces, because the
comparator nests its own bubblewrap around each kernel run, with
`COMPARATOR_BWRAP` pointing it at the same bubblewrap build as the outer
sandbox.

The submitted `enable_nanoda` field is non-authoritative and a submitted
`external_kernels` is rejected: the runner writes a protected configuration
that registers the toolchain's bundled `nanoda_bin` and `con-ron` as external
kernels (two of the five checkers `lake comparator --paranoid` would add, at
exactly the revisions the toolchain bundles, so Palomar makes no kernel
version choice of its own) and replaces only the Challenge module name with
the per-run protected alias. The comparator's exit code is read the way it is
meant: 2 is a run that could not start, and 1 is a rejection only when the
transcript carries one of the comparator's own comparison or axiom verdicts,
or Lean's own kernel refused the proof. The comparator says "rejected" for
every nonzero kernel exit, a crash included, so Lean's kernel is the arbiter:
an independent kernel failing while Lean's accepts is recorded as a kernel
disagreement for Palomar to examine, and a `bwrap:` failure of a nested
sandbox or any other unexplained stop is Palomar's to retry, never the
submitter's. Every marker is matched at the start of its own line, and
declaration names may not carry control characters, because the transcript
quotes names the submitter chose. Before any candidate code runs, the
verifier exports and judges three one-line modules of its own: a matching
pair must pass, a mismatched pair must be found against by the comparison,
and a copy of the matching export whose proof is replaced by its statement
must be refused by Lean's kernel, so a runner where the nested sandbox or a
kernel binary does not work fails closed with a Palomar-owned diagnostic.

The outer sandbox is bubblewrap 0.12.0, built from the pinned upstream release
tarball by `scripts/install_bwrap.sh` in the trusted phase (the distribution
package predates the fix for symlink handling under bind mounts, and Palomar
binds into candidate-written trees on every phase after the first). It is
started with an empty environment and receives only the named sandbox
variables, so the sandbox's PID 1 carries nothing from the runner. Before any
submitted Lean or Lake configuration runs, the verifier exercises the complete
outer policy with positive source-read and build-write probes and negative
outside-read, outside-write, sibling process-environment and outbound-network
probes, and with the namespace controls: the sandbox's PID 1 has no
environment, a nested user namespace is refused, the user namespace differs
from the host's, and canaries the verifier has just written under the host home
directory, `/tmp` and `/dev/shm` are invisible. If a positive operation is
denied, a negative operation succeeds, or the sandbox cannot be established,
verification fails closed.

bubblewrap also loads a small seccomp filter (`scripts/seccomp_filter.py`,
hand-written BPF) into every phase. It refuses `ptrace` and
`process_vm_readv`/`process_vm_writev`, so no process in a phase can read
another's memory whatever the kernel's Yama setting; it refuses `AF_UNIX`
sockets, personality changes and the creation of setuid or setgid files, which
are what the systemd unit's `RestrictAddressFamilies`, `LockPersonality` and
`RestrictSUIDSGID` used to do; and it kills a process that uses a foreign
syscall ABI. The confinement self-check exercises each of these as a negative
control. What the unit did that is not reproduced: `ProcSubset=pid`, so global
`/proc` files such as `/proc/meminfo` are readable inside the sandbox; the
process list itself is still the sandbox's own.

Resource limits and the wall-clock deadline come from a cgroup v2 subtree the
verifier owns. On a systemd host it is a `Delegate=yes` user scope; on a
container runner without systemd, `scripts/cgroup_delegate.py` runs once as
root to move the container's own processes out of the root cgroup, enable the
memory, pids and cpu controllers, enter a fresh run cgroup, hand it to the
runner user and drop privileges. `scripts/supervise_cgroup.py` then owns one
cgroup per phase: it applies `memory.max`, `pids.max` and the file limits one
level above the cgroup the workload runs in (bubblewrap's cgroup namespace is
rooted at the lower level, so the limits are out of the payload's reach even
where the kernel would let a namespace root rewrite its own controller files),
sends SIGTERM at the deadline and `cgroup.kill` after a grace period, kills
whatever the phase left behind when it ends, and writes an atomic status file
that is the only trusted account of how the phase stopped. A launcher that
failed to start, a sandbox that never reported its child, or a cgroup still
populated after the kill are infrastructure failures, never candidate results. The verifier holds a FIFO open for the phase's lifetime; if the
verifier dies, the babysitter reads end-of-file and kills the workload. The
bootstrap is proven once per verifier by running the whole chain on `true`, so
a runner that cannot delegate a cgroup fails closed before candidate code runs.

The post-acceptance renderer runs the same probe contract as the verifier,
under its own narrower policy, and fails closed on the same conditions. That
matters more there than anywhere else, because compile-time Lean in the
submitted Challenge, its macros and elaborators, execute during the render
build. Before the probe it clones the pinned Verso revision and fetches each
revision the submitted Lake manifest pins, using Git directly rather than
`lake update` so that no package post-update hook runs. After the probe there
is one narrowly scoped network-enabled confined exception for historical
ProofWidgets releases. The renderer first binds both the Mathlib and
ProofWidgets checkouts to their manifest revisions, origins, and clean Git
state, requires Mathlib's own pinned manifest to authenticate the exact
ProofWidgets revision, and recognizes only the canonical legacy release
configuration with no tracked `widget/js` tree. It then hard-links that one
read-only source into a disposable sibling workspace and runs only
`proofwidgets:release`; the disposable package's fresh `.lake` root is its sole
writable directory. Certificate and name-service files are readable, but the
merged render workspace and submitted configuration are not exposed. Afterward
the renderer rechecks every source inode, accepts exactly the expected release
archive/trace pair and a confined generated build tree, and atomically promotes
those outputs without promoting Lake configuration. Modern ProofWidgets skips
this phase. The legacy Mathlib cache client also predates the toolchain-bundled
`leantar`: the renderer reads its version and canonical release URL policy from
the same authenticated Mathlib checkout, downloads the corresponding fixed
x86-64 Linux archive with trusted credential-free `curl`, requires its fixed
SHA-256 pin, accepts only its one bounded regular executable, and checks the
reported version in a network-disabled sandbox. Its digest is rechecked after
cache discovery and the same file is preserved into cache unpacking. Trusted
`curl` separately fetches fixed-host Mathlib cache archives outside candidate
execution. All cache
discovery, unpack, render, audit, and sanitization phases remain
network-disabled. The renderer has no frozen trusted build directories, so the
verifier's frozen-write probe has nothing to assert there and is not run. Both
callers use a probe contract that removes its owned probe files even when the
sandbox runner fails.

Render metadata schema 3 adds one `audit_declarations` row for every compared
declaration. The audit executable is built from trusted source in a separate
project outside candidate-writable state. It loads the compiled Challenge with
Lean environment extensions disabled, with the root build pinned first and
candidate copies of toolchain module namespaces rejected. It then resets the
search path to the toolchain alone and copies only declaration types into a
fresh environment whose pretty-printer extensions come from that toolchain.
The JSON handoff remains outside every candidate-writable directory. The
printer disables notation unexpansion explicitly and raises its proof, depth,
and step limits; if a printer resource limit is nevertheless reached, Lean
marks the omitted subterm with `⋯`. Consequently,
submitted delaborators, unexpanders, formatters, notation, and macros cannot
choose what this secondary rendering says. The PalomarWeb schema-3 consumer
must be deployed before this producer begins publishing schema 3, because old
Web versions reject metadata versions they do not know.

This view is deliberately narrower than a semantic audit. It does not expose a
misleading instance, a silently inserted coercion, or the body hidden behind a
plausibly named definition. A reviewer still has to read the pinned source and
the definitions it uses; the core-notation rendering closes notation and macro
spoofing only.

The toolchain's `lake`, `lean`, `leanexport`, `leanchecker`, `nanoda_bin` and
`con-ron`, bubblewrap, the supervisor scripts, the protected Comparator
configuration, both exports and the verifier script are outside the writable
allowlist. Their hashes are captured before any project configuration executes
and checked before and after sandboxed phases.
The mechanical report is outside every sandbox-writable directory and is
written only by the trusted verifier after the sandboxed process exits.

### Credentials

The verification job has `contents: read` permission and is not given a write
token, App token, private-repository credential, or submission secret. Trusted
checkouts disable credential persistence, and the supervisor and bubblewrap
pass an explicit small environment-variable allowlist to untrusted processes
and reset everything else.

There is no longer a second job to compromise. Verification writes a bounded
JSON report outside every sandbox-writable directory and uploads it as the
`mechanical-report-<request_id>` artifact, and the submission server collects it
out of band. That removed a whole class of question the second job used to
raise: it held a write token, and every argument about comment ownership and
inert diagnostics existed because hostile text was being rendered by something
holding one. Nothing in this repository now holds a credential that can write
anywhere.

## Pins and trusted computing base

GitHub Actions, bubblewrap, elan releases, and source-built verifier tools are
pinned to immutable revisions or checksums. Everything that judges a submission
ships in the submitted Lean toolchain: `lake comparator`, `leanexport`,
`leanchecker` and the bundled NanoDa and con-ron kernels. The record therefore
carries the lean4 commit the toolchain's release tag names, resolved from the
tag rather than from a table (a table is a second place for the answer to be
wrong, and it kept being the wrong one), together with the sha256 of each of
those binaries as installed, the kernels the protected configuration
registered, the configuration's own digest and text, and the bubblewrap
release. The digests cover the entrypoints; the shared libraries they load
(`lake comparator` itself lives in `libLake_shared.so`) are pinned by the
release commit and the read-only toolchain binding rather than digested. The
floor in `toolchains.json` is the oldest toolchain whose comparator Palomar
has verified this way. Pin and floor changes require security review and an
end-to-end comparison probe.

This design still trusts the runner (the privileged Namespace container of the
default execution profile, or the GitHub-hosted image of the hosted profile), the Linux kernel with
its namespace, cgroup and seccomp implementations, bubblewrap, Git and its
protocol parsers, the selected Lean toolchain with its kernel, `lake
comparator`, `leanexport`, `leanchecker` and bundled independent kernels, the
Palomar verifier/reporter, the governance of the canonical allowlisted repositories,
the pinned Licensee SPDX detector and its locked Ruby dependencies,
and the contents served by Mathlib's cache service. HTTPS authenticates the
cache endpoint in transit, but the source-derived cache key is not a digest or
signature of the downloaded archive. A cache publisher or storage service able
to replace an object is therefore able to affect the compiled definitions used
by Palomar. This is an explicit trust decision, not a property established by
the sandbox. The verified implementation details and residual operational
questions are recorded in
[`docs/mathlib-cache-trust.md`](docs/mathlib-cache-trust.md).

The submission server at <https://submit.palomar-registry.org> belongs in that
list too, for part of the answer rather than all of it. It chooses the
repository, commit and paths a run is given, it chooses the ref of this
repository that the run executes, and it decides which submission the resulting
report is filed against. What it does not do is take part in the verification:
the job fetches the submitted commit without its help, and the report records
the inputs the run resolved along with the toolchain commit, the digests of the
binaries that ran and the kernels it registered. PalomarReviewer downloads the pinned artifact itself
and refuses one whose workflow revision is not in this repository's trusted
history, so the server's choice of ref is checked rather than trusted.

So what a run of a trusted revision establishes about the source it names does
not depend on the server, while what that result is attributed to does. A
compromised server could file an honest verification against the wrong
submission, or start runs nobody asked for. What it could not do is make a
trusted revision of this workflow report a pass for a project that does not
verify.

The sandbox limits effects of hostile project code; it does not make any of
these trusted components infallible.

Current protocol limits include public GitHub repositories, exactly one of
`lakefile.toml` or `lakefile.lean` in the selected project, a 500 MiB
checked-out-source cap, a 256 KiB cap on
`formalization.yaml`, a 100 KiB / 1,000-line hard cap on `Challenge.lean`, and
a 1 MiB cap on the single regular UTF-8 root licence file. Licensee reads only
that selected file, with package and README detection disabled and bounded
subprocess output and runtime. Standard fresh Lake build locations are also
required. The
verifier enforces the wall-clock allowance its caller passes, which
[`.github/workflows/submission.yml`](.github/workflows/submission.yml) sets to
19,800 seconds, five and a half hours, and applies no CPU
quota. [`verification-profile.json`](verification-profile.json) records the
`palomar-standard-v1` runner and containment policy. Each phase retains the
existing 95% memory pressure threshold and 98% ceiling, 32,768 tasks,
1,048,576 file descriptors, and 1 TiB file limit. Swap policy is unchanged;
there is no claimed Lake job-count limit. The workflow checks architecture,
minimum host memory, and free workspace before tool installation. The report
records that host snapshot and the effective percentage-based memory thresholds;
it is not a reservation or a promise about later free capacity.

Each approved execution profile supplies 330 minutes of verifier capacity
inside its 350-minute job; the report records the profile and its resolved
runner, and every phase's record carries the cgroup limits and rlimits the
supervisor actually applied on that host (`rlimits_applied` is the value after
clamping to the runner's hard limit). The cgroup supervisor's termination results identify
OOM, timeout, and resource failures; the parent's wall-clock timeout is also
trusted.
An arbitrary payload exit status or printed OOM message is not such evidence.
Missing termination telemetry is an inconclusive provider error. Resource
exhaustion retains the existing `infrastructure/resource-exhausted` outcome,
not a mathematical rejection. Inspect the reported limit and workload before
retrying: repeated exhaustion may require reducing resource use or arranging
more capacity. A transient failure may permit an unchanged retry after its
cause clears. Normal submission cooldowns apply; resource failures grant no refund.

Successful per-phase CPU, peak memory, observed task peak, elapsed time, and
approximate workspace usage are measured by an observer inside the phase
cgroup, which reads the cgroup's own peak-memory and CPU accounting (the
sandbox's PID 1 does not pass its children's `rusage` up). If that observer
dies with the workload, the parent reads the babysitter's status file and the
cgroup's memory events, then kills and removes the cgroup with a bounded
cleanup budget. Absent post-mortem cgroup files are not proof that no OOM
occurred. When the parent reaches its deadline first, the record marks
`supervisor_timeout` and leaves `systemd_result` unset, because no termination
verdict exists yet. The `systemd_result`, `systemd_active_state`,
`exec_main_code`, `exec_main_status` and `systemd_result_before_cleanup` fields
keep their names and encodings for readers of existing records (`exec_main_code`
is still `1` for an exit and `2` for a signal; `systemd_active_state` is
`failed` for a finished phase that did not succeed); their values come from the
cgroup supervisor, and every record also carries `supervisor`, `deadline_fired`,
`cpu_max_applied`, `pids_events_max` and `rlimits_applied`.
The report binds the profile id and digest; the profile
is not a submitter-selectable Comparator configuration field.

The registry database is inside that boundary too, and two of its controls are
weaker than the phrase "CI checks it" suggests. `PalomarDatabase` validates
every record and checks the append-only invariant on each pull request, but for
a `pull_request` event GitHub runs the workflow as the pull request would have
it, so "the checks passed" is a statement about that pull request's own copy of
the workflow rather than an independent verdict on it. Reading the checker from
the base revision closes the case where a pull request rewrites the checker and
leaves the workflow alone; a pull request that rewrites the workflow can decline
to read it at all. Closing that needs a required check whose implementation the
pull request cannot reach, which means one originating outside the repository,
and there is none. A force push to `main` cannot be reliably detected from
inside either: the direct before-and-after comparison runs only while the
previous tip is still fetchable, and a rewritten history is otherwise internally
consistent. The repository's activity view and GitHub's audit log are the record
for that, subject to their own retention. The append-only invariant also does
not bind at all until the launch marker is committed, which is a deliberate
pre-launch state and is announced by every CI run that finds the marker absent.
`PalomarDatabase/docs/append-only.md` states the full position and what would
close it; it is repeated here because this is the document a reader arrives at
first.

This document describes the boundary as it stands. The component review of July
2026, its adversarial evidence, repository controls, and accepted residual risks
are recorded in
[`docs/launch-security-review.md`](docs/launch-security-review.md), which is a
record of what was reviewed then and still describes the retired intake that has
since been replaced. Read it for the evidence, not for the current shape.

## Reporting a vulnerability

Please report suspected sandbox escapes, statement/provenance bypasses, verdict
forgeries, credential exposure, or other security weaknesses privately to
`kim@lean-fro.org` with the subject `[Palomar security]`. Do not open a public
GitHub issue containing exploit details.

When possible, include:

- the affected Palomar commit or workflow run;
- the impact and the security property that fails;
- a minimal, safe reproduction; and
- any proposed mitigation or disclosure constraints.

Please do not test a finding against another person's submission or against
infrastructure you do not control. We welcome coordinated disclosure and will
work with reporters to understand the problem, prepare a fix, and agree on when
technical details can safely become public.


## Approved execution profiles and recovery

The `profile` job maps an approved identifier to trusted runner labels. Inputs
cannot supply runner labels or resource limits. `palomar-standard-v1` remains the
default. `palomar-namespace-16x32-v1` remains disabled until the existing
confinement and supervision tests pass on the actual Namespace runner. The
`qualify-namespace.yml` workflow is a trusted synthetic probe and never accepts
candidate source or enables production itself.

Memory ceilings use the minimum of host RAM and visible ancestor cgroup limits;
CPU evidence uses affinity and ancestor quotas. Workload limits are absolute
bytes derived from that effective memory, preventing a container from sizing
its worker against the underlying host's RAM. Verification keeps the existing
execution budget and job timeout. Rendering inherits an explicitly selected
profile without extending its deadlines.

The stdlib finalizer can report a failed dependency installation without loading
verifier dependencies. It preserves source-bound terminal diagnoses, finalizes
interrupted pending reports, and records the selected profile and operator
attempt. Upload may be attempted three times; the final gate requires successful
report delivery as well as a passing verifier (or prepared preflight). Runner
loss before finalization is diagnosed downstream only from the trusted recorded
GitHub run and job attempt. Candidate log text cannot establish an OOM.

Pinned elan downloads and immutable trusted-tool Git fetches retry transient
network failures at most three times within a five-minute operation budget and
the original job allowance. Checksum, authentication, absent revision, and build
failures do not restart verification. Progress reports preserve semantic stages
and trusted resource measurements for interrupted runs.

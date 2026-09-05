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
modify Comparator or another verifier tool, overwrite the mechanical report,
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
explicit triggers, `workflow_dispatch` and `workflow_call`; no push, comment,
or pull request starts one. The server dispatches authoritative registry runs.
A public repository may call the same job as a predictive mechanical preflight,
but that caller has no Palomar state or credential and cannot register its
result. There is one job, `verify`, and its `permissions` block is
`contents: read`. Its inputs are a repository, a commit, a pinned pipeline
commit for reusable calls, an opaque submission id, a closed
`preflight`/`full` mode, and a JSON object whose keys are checked against the fixed `OPTIONAL_FIELDS`
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

### Landrun confinement

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
an unpredictable per-run top-level alias. Comparator's
`LEAN_PATH` resolves that directory, Lean core, and every frozen trusted build
directory before all candidate build paths. Candidate Lake
configuration can still build arbitrary proof dependencies in its own fresh
directories, but it cannot replace the statement module or a trusted dependency
used to compile it. A nonstandard output layout fails closed.

Every invocation that can load project Lake configuration runs under the same
outer Landrun policy. This includes Mathlib cache retrieval, `lake env` used to
discover Lean paths, and Comparator itself. There is no blanket read rule for
the runner filesystem. The policy grants read-only access to the submitted
source tree and a small explicit set of certificate/name-service files,
read/execute access to the selected Lean toolchain, pinned verifier programs,
and immutable system/Python runtime directories, and write/execute access only
to the fresh build and Lake-configuration directories. Unrelated runner
temporary directories, home-directory contents, the report, and sibling
process state are outside the read allowlist. Sandboxed Git ignores system and
global configuration and cannot prompt for credentials, preventing ambient
runner configuration from rewriting authenticated remotes. The protected
Landrun adapter injects the same fixed Git isolation into Comparator's nested
challenge and solution build domains.

Normal configuration and comparison also run in a systemd private network
namespace; Landrun independently restricts supported TCP operations. The
verified Mathlib cache client has a narrowly scoped exception because
downloading official cache artifacts is its purpose. During that phase Mathlib
is the workspace root and its exact official closure is exposed through
temporary package links. The links are deleted immediately afterward, the
trusted cache output is frozen, and candidate Lake configuration is not loaded
while network access is available.

Comparator continues to use its own Landrun domains for its separate challenge,
solution, export, and NanoDa replay operations. The submitted `enable_nanoda`
field is non-authoritative: the runner writes a protected configuration that
always enables NanoDa and replaces only the Challenge module name with the
per-run protected alias before executing candidate code. Comparator's redundant
`lake build` of that exact alias is skipped by the trusted adapter because the
verifier has already compiled it outside the candidate Lake plan.
Linux Landlock domains compose by intersection:
the inner policy cannot widen the outer policy's filesystem or network access.
The pinned Landrun binary is built without cgo so its pre-Landlock-v8
all-thread enforcement does not enumerate `/proc/$PID/task`; nested confinement
therefore remains compatible with the outer process-read denial.
The outer Landrun process is launched in an unprivileged systemd unit that also
applies the private-network and address-family policy, a private device and
temporary-file view, `NoNewPrivileges`, and process-information hiding.
Landrun uses compatibility mode across GitHub runner kernel versions, so the
verifier first exercises the complete outer policy with positive source-read
and build-write probes and negative outside-read, outside-write, sibling
process-environment, and outbound-network probes. It also runs a positive nested
Landrun probe. If a positive operation is denied, a negative operation succeeds,
or either confinement layer cannot be established, verification fails closed.

Compatibility mode is what makes those probes load-bearing rather than
decorative. The pinned Landrun asks the kernel to handle every access right
up to Landlock ABI v9, including the v9 Unix-socket resolution right that the
runner kernels do not yet provide. Refusing the downgrade would therefore not
make the boundary strict, it would make every confined command fail to start
on every host in use. The downgrade is measured instead: rights the kernel
does not support are dropped silently, and a policy degraded far enough to
permit a denied read, write, or connection is caught by these probes before
any submitted Lean or Lake configuration runs. Landlock being absent
altogether degrades the policy to nothing, which the write and read denial
probes reject.

The post-acceptance renderer runs the same probe contract as the verifier,
under its own narrower policy, and fails closed on the same conditions. That
matters more there than anywhere else, because compile-time Lean in the
submitted Challenge, its macros and elaborators, execute during the render
build. The renderer has no network-enabled confined phase to except. Every
outbound step it takes is a trusted one outside Landrun: before the probe it
clones the pinned Verso revision and fetches each revision the submitted Lake
manifest pins, using Git directly rather than `lake update` so that no package
post-update hook runs; after the probe, trusted `curl` fetches Mathlib cache
archives. None of them load submitted Lake configuration, and every phase
Landrun confines, on both sides of that download, is network-disabled, so
outbound-network denial is proved for each of them. The renderer has no frozen
trusted build directories, so the verifier's frozen-write probe has nothing to
assert there and is not run. Both callers use a probe contract that removes
its owned probe files even when the sandbox runner fails.

Comparator, `lean4export`, NanoDa, Landrun, the Landrun adapter, Lake, the
protected Comparator configuration, and the verifier script are outside the
writable allowlist. Their hashes are captured before any project configuration
executes and checked before and after sandboxed phases.
The mechanical report is outside every sandbox-writable directory and is
written only by the trusted verifier after the sandboxed process exits.

### Credentials

The verification job has `contents: read` permission and is not given a write
token, App token, private-repository credential, or submission secret. Trusted
checkouts disable credential persistence, and Landrun passes an explicit small
environment-variable allowlist to untrusted processes through the systemd unit
and its own environment filter.

There is no longer a second job to compromise. Verification writes a bounded
JSON report outside every sandbox-writable directory and uploads it as the
`mechanical-report-<request_id>` artifact, and the submission server collects it
out of band. That removed a whole class of question the second job used to
raise: it held a write token, and every argument about comment ownership and
inert diagnostics existed because hostile text was being rendered by something
holding one. Nothing in this repository now holds a credential that can write
anywhere.

## Pins and trusted computing base

GitHub Actions, Comparator, Landrun, NanoDa, elan releases, and source-built
verifier tools are pinned to immutable revisions or checksums. NanoDa uses the
`robsimmons/nanoda_lib` fork deployed by Comparator Live at commit
`68d5ca9db226849b41a6fff59d796ff19d0a8840`. A `lean4export` revision is resolved
from the submitted toolchain's own release tag rather than from a table, because
a table is a second place for the answer to be wrong and it kept being the wrong
one. Pin changes require security review and an end-to-end comparison probe.

This design still trusts the GitHub-hosted Linux runner, the Linux kernel and
Landlock implementation, systemd, Git and its protocol parsers, the selected
Lean toolchain and kernel, Comparator, `lean4export`, Landrun, the Palomar
verifier/reporter, NanoDa's independent kernel, the governance of the canonical allowlisted repositories,
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
the inputs the run resolved along with the Comparator, `lean4export`, NanoDa and
Landrun revisions that ran. PalomarReviewer downloads the pinned artifact itself
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
quota. [`verification-profile.json`](verification-profile.json) defines the
single accepted `palomar-standard-v1` envelope: a fixed GitHub-hosted runner
label, absolute memory ceilings, minimum free workspace, fixed Lake
parallelism, and the task, descriptor, file-size, and wall-clock limits. The
workflow checks host capacity before candidate execution, and every mechanical
report binds the profile id, contents digest, runner, and limits.

The automatic GitHub-hosted tier currently supplies 330 minutes of verifier
capacity inside its 350-minute job. Reaching that capacity, an OOM ceiling, a
task/file ceiling, or disk exhaustion produces the explicit retryable outcome
`infrastructure/resource-exhausted` and never a mathematical rejection or
changes-requested result. The report includes bounded per-phase elapsed time,
CPU time, maximum resident memory, observed task peak, and approximate peak
workspace disk consumption. Resource supervision runs outside the confined
unit, then reads the unit's result and cgroup memory events before collection;
an OOM therefore cannot kill the only observer capable of classifying it.
Lack of a worker satisfying the profile leaves verification inconclusive rather
than changing what Palomar accepts.

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

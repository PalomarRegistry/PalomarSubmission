# Palomar security policy

Palomar mechanically checks Lean projects supplied through GitHub issues. A
submission is untrusted input, not merely untrusted Lean source. This document
describes the threat model, the verification boundary, and how to report a
problem without exposing other users to it.

## Threat model

The submitter controls the issue fields and every byte of the referenced Git
commit. In particular, the following are treated as potentially hostile:

- `Challenge.lean`, `Solution.lean`, and any other source file;
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

### Intake and dependency provenance

- The verifier accepts only a public, credential-free
  `https://github.com/owner/repository` URL and a full 40-character commit SHA.
  Dynamic issue values are passed as files or subprocess arguments, not
  interpolated into shell programs.
- Git dependencies are materialized directly at the full commits recorded in
  the submitted manifest. Git hooks, global/system Git configuration, local
  transport, and interactive credential prompts are disabled. The verifier
  does not run `lake update` or dependency post-update hooks.
- An allowlisted Mathlib or Tau Ceti revision is trusted only if Git proves that
  it is an ancestor of the configured branch in the canonical repository.
  A compatibility exception may name an exact historical commit explicitly;
  it does not make adjacent history trusted. Known repository moves are handled
  as explicit aliases, not as arbitrary URL equivalence.
- The allowlisted root's own `lake-manifest.json` is authoritative for its
  pinned dependency closure. Every package name, canonical repository, and
  revision in that closure must match the submission's flattened manifest.
  Reusing a trusted package name while substituting a fork or commit is rejected.
- The transitive source closure of `Challenge.lean` may contain only Lean core
  or the verified pinned closure of an allowlisted root. Candidate-local helper
  imports are rejected in the current protocol. Palomar-indexed provenance is
  recorded by the metadata model, but it is not accepted as executable
  Challenge input until Palomar can reconstruct that earlier statement surface
  independently. The closure comes from Lean's own `--src-deps` result, and
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
authenticated cache is run with official Mathlib as the workspace root and
with symlinks to the exact independently verified official closure. Other
allowlisted roots are built from their own verified configuration. Candidate
Lake configuration never runs during either operation. Trusted-root Lake URL
resolution is pinned to that root's authenticated manifest and is accepted only
when the flattened submission manifest names the same canonical GitHub
repository. The official
Mathlib plan also replays the downloaded cache and receives write access to the
one exact ProofWidgets replay-marker file it generates; qualified roots receive
no write access to Mathlib source or build output. Trusted build directories are
then frozen read/execute-only. The verifier compiles `Challenge.lean`
directly with trusted Lean against only those frozen dependencies, outside the
candidate's Lake plan, records the resulting `Challenge.olean` digest, and
copies only that one module into a fresh protected directory. Comparator's
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
process state are outside the read allowlist.

Normal configuration and comparison also run in a systemd private network
namespace; Landrun independently restricts supported TCP operations. The
verified Mathlib cache client has a narrowly scoped exception because
downloading official cache artifacts is its purpose. During that phase Mathlib
is the workspace root and its exact official closure is exposed through
temporary package links. The links are deleted immediately afterward, the
authenticated output is frozen, and candidate Lake configuration is not loaded
while network access is available.

Comparator continues to use its own Landrun domains for its separate challenge,
solution, and export operations. Linux Landlock domains compose by intersection:
the inner policy cannot widen the outer policy's filesystem or network access.
The outer Landrun process is launched in an unprivileged systemd unit that also
applies the private-network and address-family policy, a private device and
temporary-file view, `NoNewPrivileges`, and process-information hiding.
Landrun uses compatibility mode across GitHub runner kernel versions, so the
verifier first exercises the complete outer policy with positive source-read
and build-write probes and negative outside-read, outside-write, sibling
process-environment, and outbound-network probes. If a positive operation is
denied, a negative operation succeeds, or either confinement layer cannot be
established, verification fails closed.

Comparator, `lean4export`, Landrun, the Landrun adapter, Lake, and the verifier
script are outside the writable allowlist. Their hashes are captured before any
project configuration executes and checked before and after sandboxed phases.
The mechanical report is outside every sandbox-writable directory and is
written only by the trusted verifier after the sandboxed process exits.

### Credentials and reporting separation

The verification job has `contents: read` permission and is not given an issue
token, App token, private-repository credential, or submission secret. Trusted
checkouts disable credential persistence, and Landrun passes an explicit small
environment-variable allowlist to untrusted processes through the systemd unit
and its own environment filter.

The later reporting job is separate. It receives issue-write permission but
does not check out or execute the submitted project; it reads only the bounded
JSON artifact produced by the verification job. Its authority target comes
from the trusted workflow event and must agree with the issue number recorded
in the artifact. It updates only a marker comment owned by
`github-actions[bot]`; a submitter-authored lookalike cannot claim that slot.
Build diagnostics are rendered as inert code rather than Markdown structure.

## Pins and trusted computing base

GitHub Actions, Comparator, Landrun, elan releases, and source-built verifier
tools are pinned to immutable revisions or checksums. `lean4export` revisions
are mapped to exact Lean toolchain releases in `toolchains.json`. Pin changes
require security review and an end-to-end comparison probe.

This design still trusts the GitHub-hosted Linux runner, the Linux kernel and
Landlock implementation, systemd, Git and its protocol parsers, the selected
Lean toolchain and kernel, Comparator, `lean4export`, Landrun, the Palomar
verifier/reporter, and the governance of the canonical allowlisted repositories.
The sandbox limits effects of hostile project code; it does not make those
components infallible.

Current protocol limits include public GitHub repositories, `lakefile.toml` at
the submission root, a 500 MiB checked-out-source cap, a 100 KiB / 1,000-line
hard cap on `Challenge.lean`, standard fresh Lake build locations, a
180-minute comparison timeout, and a 330-minute job-wide wall-clock deadline
(including trusted setup) within a 350-minute job budget. Verification
returns an infrastructure error or rejection—not a best-effort pass—when a
toolchain is unsupported, provenance is ambiguous, confinement is unavailable,
or execution times out.

The current pre-launch component review, adversarial evidence, repository
controls, and accepted residual risks are recorded in
[`docs/launch-security-review.md`](docs/launch-security-review.md).

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

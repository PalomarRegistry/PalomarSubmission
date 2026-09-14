# Pre-launch security review record, 2026-07-31

## Superseded facts, as of 2026-08-08

This is the record of the launch-hardening work as it was reviewed on
2026-07-31. It is not a description of what runs now. `SECURITY.md` is that, and
where the two disagree `SECURITY.md` is right. Four things have changed since:

- **Intake.** Submissions no longer arrive as GitHub issues. The submission
  server at <https://submit.palomar-registry.org> dispatches the verification
  workflow, and the second job that bound a result back to an issue is gone
  along with the write token it held.
- **Publication.** `PalomarDatabase` is private, its active-only projection is
  published to private R2, and `data.palomar-registry.org` is a read-only Worker
  serving one document per question, with no whole-registry index.
- **Scores.** Review scores were moved out of the record into a private
  `scores/` file, which is why there is one record schema again rather than a
  canonical and a public one.
- **Branch protection.** The private repository is on GitHub Free and therefore
  no longer has enforced branch protection; its append-only controls are CI plus
  maintainer procedure until the organization upgrades to GitHub Team.

Where the sections below describe the retired intake or the second job that
reported into it, read them as a record of what was reviewed in July.

This record covers the launch hardening tracked by
[`PalomarSubmission#5`](https://github.com/PalomarRegistry/PalomarSubmission/issues/5).
It records the implemented boundary, the checks used to exercise it, and the
risks intentionally left for separate design work. The pull requests linked
from the tracker are the authoritative diffs.

## Submission boundary

The final verifier treats the request, the submission checkout, every dependency,
Lake configuration, generated process, build diagnostic, and committed build
artifact as hostile. Its security-relevant sequence is:

1. Parse the dispatched request fields, against an exact allowlist of optional
   keys, and fetch the submitted full commit without credentials, hooks, local
   Git transport, or ambient Git config.
2. Delete submitted Lake state and independently materialize every full manifest
   revision without running `lake update` or post-update hooks.
3. Verify canonical Mathlib/Tau Ceti ancestry (or an exact, reviewed legacy
   commit) and the root's exact pinned manifest closure.
4. Fetch and replay Mathlib's trusted cache from the official Mathlib
   workspace, then freeze the high-trust closure. Recreate every root-owned Lake
   tree before building a qualified root, without granting it write access to
   the Mathlib-owned closure or its verifier-created links, then freeze its
   output.
5. Compile `Challenge.lean` directly with trusted Lean against frozen
   allowlisted output. Snapshot its `Challenge.olean` and audit Lean's source
   dependency list and source bytes before candidate Challenge/Solution
   compilation and comparison.
6. Build the candidate Challenge/Solution and run Comparator under the explicit
   outer Landrun/systemd boundary with a verifier-authored protected
   configuration that forces `"enable_nanoda": true` and replaces only the
   Challenge module name with the canonical alias.
   Publish the protected Challenge under a collision-resistant verifier-owned
   top-level module alias, then resolve that alias, Lean core, and frozen trusted
   modules before candidate paths in Comparator's `LEAN_PATH`; candidate output
   cannot replace any of them, and the protected root cannot capture a sibling
   Solution under the submitted Challenge namespace. Require both Lean's kernel
   and the pinned independent NanoDa kernel to accept the exported proof.
7. Write the bounded report after sandboxed execution, outside every
   sandbox-writable directory, and upload it as a run artifact. There is no
   second job: the submission server collects the artifact, so nothing here
   renders hostile diagnostics while holding a write token.

The outer filesystem policy has no `--ro /` or broad home/workspace rule. Live
tests establish permitted source reads and build writes, reject sibling reads
and writes, hide another process's environment, and deny normal-phase network
access. The Mathlib cache operation is the only network exception and never
loads candidate Lake configuration.

## Compatibility and adversarial evidence

The maintained test surfaces are:

- `tests/test_verify_submission.py`: current provenance and exact tool pins,
  official closure substitution, duplicate form sections, artifact rejection,
  protected paths, environment path bounds, and systemd policy construction;
- `tests/test_sandbox_integration.py`: real Landrun/systemd read, write, process,
  and network probes plus direct canonical Challenge compilation;
- `tests/test_render_challenge.py` and `tests/test_landrun_passthrough.py`:
  the pinned Verso rendering path and the sandbox flags it is given;
- `tests/test_compatibility_workflow.py`: the merge-base scope decision and the
  required final gate, including renames, deletions, unusual paths, and failed
  classification or build jobs;
- `.github/workflows/compatibility.yml`: a cold production-like run of the
  checked multi-dependency fixture in `tests/fixtures/cold-tauceti`, followed by
  the real pinned Comparator, toolchain-matched `lean4export`, and pinned NanoDa
  under the nested sandbox. Before the real cache phase, that route plants an
  ignored executable in Mathlib and one closure dependency, then requires the
  production cache boundary to remove both. It also plants an executable in the
  qualified Tau Ceti root and requires the trusted-root boundary to remove it
  before the real build. The same checked fixture gives the Challenge a dotted
  module path and a private declaration, then runs the production renderer and
  requires the original-module `.olean` and raw Verso page while retaining the
  stable public `Challenge/index.html` artifact. The ordinary pull-request check
  byte-compares the checked `formalization.yaml` and `comparator.json`
  contracts with Template commit
  `d720f59dbe2edd29e0b9273c113139cdb1f24d2b`; scheduled and manual checks
  reconcile those two files with Template `main`. The cold job
  uses the hosted tier's 330-minute capacity measured from the start of its
  350-minute job, so setup time and candidate execution consume one allowance.
  The verifier's trusted default supports twelve hours on a suitably configured
  worker; hosted-tier exhaustion is retryable infrastructure, not rejection.
  Pull requests first classify their merge-base diff. Only changes confined to
  `README.md`, `SECURITY.md`, `LICENSE`,
  `docs/comparator-declaration-closure.md`,
  `docs/launch-security-review.md`, `docs/mathlib-cache-trust.md`,
  `taxonomies/README.md`, and `taxonomies/LICENSE.md` skip the cold build. A new
  documentation path is not implicitly trusted. Every non-pull-request event
  runs the cold build, including manual dispatch and the weekly compatibility
  canary. The required `compatibility` gate passes only when classification
  succeeds and either the selected cold build succeeds or the change is
  explicitly confined to that exact prose set.

The cold fixture uses Lean `v4.31.0-rc2`, the toolchain fixed by the accepted
Tau Ceti revision, exact Comparator boolean
`"enable_nanoda": true`, and ten pinned project packages. Its canonical
Challenge imports only Mathlib. Its candidate Solution imports the Tau Ceti
root at accepted revision `221bb56a017bb794421eac4fa543d7a5e85add75`, so the
run verifies the qualified trusted root and its exact flattened dependency
closure and compiles Tau Ceti's broad module graph. The same run exercises
canonical Challenge compilation, source provenance audit, the complete
confinement probe set, candidate Challenge/Solution builds, and comparison
through both kernels. CI materializes the checked files as a clean temporary Git
checkout to mirror the production checkout boundary; it does not rewrite the
submitted configuration.

Each trusted-state reset indexes the submitted manifest once, validates the
selected package paths once, and removes each selected Lake-state entry once.
For `P` manifest packages, `R` packages owned by the boundary, and `S` ignored
state entries, the extra work is `O(P + R + S)` local filesystem work and
`O(R)` temporary path storage. It adds no network request, cache archive,
dependency build, or service. The canonical-role uniqueness check reuses the
allowlist's existing root/package scan and adds no new asymptotic term.

This fixture does not cold-build Template's current `v4.32.0` project,
`lakefile.toml`, or `lake-manifest.json`. The Template checks above establish
the current authoring metadata and Comparator bytes, not current-toolchain
execution compatibility. Runtime coverage here is instead exact for the older
toolchain and dependency graph required by the accepted Tau Ceti snapshot. The
pull-request workflow must reproduce that result on the supported GitHub-hosted
runner before merging any execution-affecting change. The explicit prose-only
gate is sufficient for the documentation paths described above.

## Component review

### Comparator

The workflow pins Comparator commit
`575674928e239f5bc452aab72d1dd7b0f1326494`. The reviewed path separately
exports Challenge and Solution environments, checks configured declarations and
their dependency closures, enforces the permitted-axiom set, and replays the
comparison through both Lean's kernel and the pinned independent NanoDa kernel.
The submitted `enable_nanoda` compatibility field is non-authoritative: the
verifier forces the exact JSON boolean `true` in the protected configuration
that Comparator consumes. It also replaces the submitted Challenge module name
with the canonical alias while leaving the Solution and declaration selection
unchanged. Comparator's own Landrun domains remain in place.
They are nested inside Palomar's outer domain, so they can narrow but not widen
Palomar's filesystem or network policy. Palomar independently protects
the Challenge module because Comparator assumes the supplied Challenge build is
the intended statement.

### Reviewer and policy

PalomarReviewer accepts a mechanical report only from the run the submission
server pinned, frames each submission/model evidence item as hostile JSON with a
digest, and repeats the evidence boundary after the hostile material. Public
model prose is Markdown-inert. `--apply` delivers only a previously stored
review and revalidates the submission, source, report, schema, and pinned policy
revision; it does not rerun a model at apply time. PalomarPolicy prompts
explicitly reject instructions embedded in formalization metadata,
Lean/repository text, submission context, literature evidence, or prior model
output.

### Database and website

PalomarDatabase enforces effective URI/date-time formats, canonical GitHub
evidence URLs, cross-field source/submission/trust consistency, safe relative
paths, and immutable `(id, version)` records. Its complete ledger and takedown
state remain private; a validated active-only projection is published to R2 and
exposed through a Worker that allowlists read-only public paths, with records at
keys that never change and only the aggregates under a release. PalomarWeb
pins that production registry and render origin, restricts development
overrides to loopback, validates the supported schema/status/verdict and
selected identity, rejects duplicate or escaping summaries, centralizes safe
links, applies CSP, and displays the Challenge/Solution digests.

## Repository controls

The protected default branches of PalomarSubmission, PalomarWeb,
PalomarReviewer, and PalomarPolicy require their configured reviews and checks;
force pushes and branch deletion are disabled by their repository controls.
`PalomarDatabase` is the exception: GitHub Free supports those controls for
public organization repositories but not private ones. Its `validate` and
`append-only` jobs still detect violations, and maintainers must use current
pull requests, wait for both jobs, squash merge, and avoid direct/force pushes
or branch deletion. Upgrade the organization to GitHub Team and restore the
enforced Database protection described in
`PalomarDatabase/docs/append-only.md` when practical. Database publication
remains append-only for existing versioned record paths.

## Accepted residual risks and deferred work

- Verification is dispatched automatically by the submission server, so compute
  abuse is bounded there rather than here: a submitter must prove push access to
  the repository being submitted, must wait out an interval between starts, and
  the server admits only a bounded number of submissions at once. This workflow
  no longer adds a bound of its own. Its concurrency group is per submission,
  because the single literal group it used to share serialised every submission
  ever made and cancelled the run waiting behind the one in progress, which cost
  availability without tightening the bound the server already applies.
- Database pull requests run both the proposed validator and the validator from
  the base revision. Runtime/CSP pins are append-only compatibility data, while
  the existing base-owned append-only checker continues to judge record history.
- Arbitrary pinned public Git dependencies remain supported for proof code.
  They execute only in candidate-writable build/configuration directories and
  cannot enter the protected Challenge source surface.
- The committed-artifact suffix scan is a compatibility rejection, not a
  complete classifier. Statement integrity rests on canonical compilation and
  frozen trusted output, not filename recognition.
- Canonical allowlisted repository governance and the exact legacy Tau Ceti pin
  are part of the trusted computing base. Adding a root, alias, official ref, or
  legacy commit requires review and a cold compatibility run.
- A project Palomar has already accepted is not thereby an allowed Challenge
  input. The allowlisted roots are the only statement dependencies, and a
  recursively reached source outside them remains forbidden.
- GitHub-hosted runners, Linux/Landlock/systemd, Git, Lean and its kernel,
  Comparator, NanoDa, Landrun, `lean4export`, and the Palomar implementation remain in
  the trusted computing base. This hardening is defense in depth around those
  components, not a proof that they are bug-free.

Signed registry snapshots, a transparency log, a content-addressed certificate
architecture, frontend reimplementation of provenance, cross-version semantic
continuity, and general support for arbitrary custom Lake output layouts remain
out of scope for this launch gate.

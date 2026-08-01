# Pre-launch security review record

Date: 2026-07-31

This record covers the launch hardening tracked by
[`PalomarSubmission#5`](https://github.com/kim-em/PalomarSubmission/issues/5).
It records the implemented boundary, the checks used to exercise it, and the
risks intentionally left for separate design work. The pull requests linked
from the tracker are the authoritative diffs.

## Submission boundary

The final verifier treats the issue, the submission checkout, every dependency,
Lake configuration, generated process, build diagnostic, and committed build
artifact as hostile. Its security-relevant sequence is:

1. Parse a unique set of issue-form fields and fetch the submitted full commit
   without credentials, hooks, local Git transport, or ambient Git config.
2. Delete submitted Lake state and independently materialize every full manifest
   revision without running `lake update` or post-update hooks.
3. Verify canonical Mathlib/Tau Ceti ancestry (or an exact, reviewed legacy
   commit) and the root's exact pinned manifest closure.
4. Fetch and replay Mathlib's trusted cache from the official Mathlib
   workspace, then freeze the high-trust closure. Build qualified roots without
   granting them write access to that closure, then freeze their output.
5. Independently rebuild exact Palomar-indexed dependency snapshots, freeze
   their output, then compile `Challenge.lean` directly with trusted Lean
   against frozen allowlisted and indexed output. Snapshot its
   `Challenge.olean` and audit Lean's source dependency list and source bytes
   before candidate Lake configuration executes.
6. Build the candidate Challenge/Solution and run Comparator under the explicit
   outer Landrun/systemd boundary. Resolve the protected Challenge module, Lean
   core, and frozen trusted modules before candidate paths in Comparator's
   `LEAN_PATH`; candidate output cannot replace any of them.
7. Write the bounded report after sandboxed execution. A separate reporting job
   binds it to the triggering issue and renders hostile diagnostics inertly.

The outer filesystem policy has no `--ro /` or broad home/workspace rule. Live
tests establish permitted source reads and build writes, reject sibling reads
and writes, hide another process's environment, and deny normal-phase network
access. The Mathlib cache operation is the only network exception and never
loads candidate Lake configuration.

## Compatibility and adversarial evidence

The maintained test surfaces are:

- `tests/test_verify_submission.py`: provenance ancestry and exact legacy pins,
  official closure substitution, duplicate form sections, artifact rejection,
  protected paths, environment path bounds, and systemd policy construction;
- `tests/test_sandbox_integration.py`: real Landrun/systemd read, write, process,
  and network probes plus direct canonical Challenge compilation;
- `tests/test_report_issue.py`: trusted issue binding, bot-owned marker selection,
  and Markdown-inert hostile diagnostics;
- `.github/workflows/compatibility.yml`: a cold production-like run of the
  accepted `erdos-unit-distance-comparator` fixture at commit
  `8d9b8319a4ed2dd094655978e905512dee6394b6`, including its Mathlib, Tau Ceti,
  Lake-file-based, and ordinary proof dependencies, followed by the real pinned
  Comparator and toolchain-matched `lean4export` under the nested sandbox. It
  uses the hosted tier's 330-minute capacity measured from the start of its
  350-minute job, so setup time and candidate execution consume one allowance.
  The verifier's trusted default supports twelve hours on a suitably configured
  worker; hosted-tier exhaustion is retryable infrastructure, not rejection.

The cold fixture passed locally with Lean `v4.31.0-rc2`, Landrun, 16 pinned
project dependencies, canonical Challenge compilation, source provenance
audit, the complete confinement probe set, and candidate Challenge/Solution
builds and comparison. The pull-request workflow must reproduce that result on
the supported GitHub-hosted runner before merge.

## Component review

### Comparator

The workflow pins Comparator commit
`68a064109f01c08f47c8edc9f51d6a2bbffaa188`. The reviewed path separately
exports Challenge and Solution environments, checks configured declarations and
their dependency closures, enforces the permitted-axiom set, and replays the
comparison through Lean's kernel. Comparator's own Landrun domains remain in
place. They are nested inside Palomar's outer domain, so they can narrow but not
widen Palomar's filesystem or network policy. Palomar independently protects
the Challenge module because Comparator assumes the supplied Challenge build is
the intended statement.

### Reviewer and policy

PalomarReviewer accepts a mechanical report only from the GitHub Actions bot,
frames each submission/model evidence item as hostile JSON with a digest, and
repeats the evidence boundary after the hostile material. Public model prose is
Markdown-inert. `--apply` posts only a previously inspected dry-run artifact and
revalidates the issue, source, report, schema, and pinned policy revision; it
does not rerun a model at apply time. PalomarPolicy prompts explicitly reject
instructions embedded in formalization metadata, Lean/repository text, issue
context, literature evidence, or prior model output.

### Database and website

PalomarDatabase enforces effective URI/date-time formats, canonical GitHub
evidence URLs, cross-field source/issue/trust consistency, safe relative paths,
and immutable `(id, version)` records. PalomarWeb pins production registry and
render sources, restricts development overrides to loopback, validates the
supported schema/status/verdict and selected identity, rejects duplicate or
escaping summaries, centralizes safe links, applies CSP, and displays the
Challenge/Solution digests.

## Repository controls

The default branches of PalomarSubmission, PalomarDatabase, PalomarWeb,
PalomarReviewer, and PalomarPolicy require up-to-date CI, one approving review,
stale-review dismissal, conversation resolution, and administrator enforcement.
Force pushes and branch deletion are disabled. Required checks are `test` and
the cold `compatibility` run for Submission, `test` for Web/Reviewer,
`validate` and `append-only` for Database, and `validate` for Policy. Database
publication remains append-only for existing versioned record paths.

## Accepted residual risks and deferred work

- Automatic issue-triggered verification remains enabled. Compute abuse and
  rate limiting are tracked separately in issue #3.
- The database workflow still runs the validator from the proposed revision.
  The base-revision validator/migration design (D5) is intentionally deferred;
  required human review, CI, and append-only checks are its current operational
  backstops.
- Arbitrary pinned public Git dependencies remain supported for proof code.
  They execute only in candidate-writable build/configuration directories and
  cannot enter the protected Challenge source surface.
- The committed-artifact suffix scan is a compatibility rejection, not a
  complete classifier. Statement integrity rests on canonical compilation and
  frozen trusted output, not filename recognition.
- Canonical allowlisted repository governance and the exact legacy Tau Ceti pin
  are part of the trusted computing base. Adding a root, alias, official ref, or
  legacy commit requires review and a cold compatibility run.
- Palomar-indexed dependencies are executable Challenge inputs only at an exact
  recorded ID/version, repository, and commit. Their source is reconstructed
  independently, recursive unindexed imports remain forbidden, and their
  imported definitions are included in editorial review.
- GitHub-hosted runners, Linux/Landlock/systemd, Git, Lean and its kernel,
  Comparator, Landrun, `lean4export`, and the Palomar implementation remain in
  the trusted computing base. This hardening is defense in depth around those
  components, not a proof that they are bug-free.

Signed registry snapshots, a transparency log, a content-addressed certificate
architecture, frontend reimplementation of provenance, cross-version semantic
continuity, and general support for arbitrary custom Lake output layouts remain
out of scope for this launch gate.

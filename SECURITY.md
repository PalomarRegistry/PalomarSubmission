# Security model

## Threat

The submitter controls the issue fields and every byte of the referenced Git
commit, including Lean source, Lake configuration, dependency repositories, and
compiled artifacts committed to Git. Assume deliberate attempts to escape the
build, steal credentials, falsify the compared statement, exhaust resources, or
inject shell syntax.

## Boundary

- The `verify` job has `contents: read` only. It receives no private repository
  token, issue-write token, App token, or other secret.
- Both trusted checkouts use `persist-credentials: false`.
- Dynamic issue values are passed as files or subprocess arguments, never
  interpolated into shell programs.
- Only a public `https://github.com/owner/repo` URL and a full commit SHA are
  accepted.
- Comparator builds `Challenge` and `Solution` separately under landrun, checks
  declaration identity and the axiom allowlist, and replays the solution through
  the Lean kernel.
- Comparator is additionally launched in an unprivileged systemd unit using
  Comparator's documented address-family restriction. Failure to establish
  this confinement fails closed.
- Git dependencies are materialized directly from the submitted manifest at
  exact full commits. The verifier does not run `lake update` or dependency
  post-update hooks.
- The `report` job receives an issue-write token but only reads the trusted
  workflow's bounded JSON artifact. It does not check out or execute submission
  source.

## Dependency rule

The solution project may depend on arbitrary repositories. This is necessary for
Palomar to index results from ordinary Lean developments without forcing those
developments into a registry-specific shape.

The transitive source closure of `Challenge.lean` is different: every source
must belong to Lean core, the pinned closure of an allowlisted Mathlib/Tau Ceti
package, or the exact commit of a repository already indexed in
`PalomarDatabase`. Candidate-local helper imports are rejected; put the
human-auditable statement and any new statement definitions directly in
`Challenge.lean`.

Package names are not trusted. The verifier resolves imports against Lake's
source search path, maps dependency source paths through `lake-manifest.json`
to normalized repository URLs and resolved revisions, and recursively follows
imports from Palomar-indexed packages.

## Pins

Every GitHub Action, Comparator, landrun, and elan release is pinned to immutable
content. `lean4export` commits are mapped to exact Lean toolchain releases in
`toolchains.json`. Pin bumps require a security review and an end-to-end
comparator probe.

## Deliberate v1 limits

- public GitHub repositories only;
- `lakefile.toml` only;
- 500 MiB checked-out source cap;
- 100 KiB / 1,000-line hard cap on `Challenge.lean`;
- GitHub-hosted Linux runner and a 330-minute verification timeout;
- no source archive retention and no private-submission credentials.

If confinement is unavailable, a toolchain is unsupported, dependency provenance
cannot be resolved, or the job times out, the result is an infrastructure error
or rejection—not a best-effort pass.

Report suspected sandbox or soundness vulnerabilities privately to the repository
owner rather than opening a public issue with exploit details.

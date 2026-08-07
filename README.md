# Palomar Submission

Mechanical verification for the Palomar registry.

[**Submit a Lean-verified result →**](https://submit.palomar-registry.org)

The form asks for a public GitHub repository, an immutable commit, and the
repository-relative path of exactly one Comparator configuration. The
repository itself carries the metadata. One configuration becomes one Palomar
entry; several configurations at the same repository and commit are submitted
separately, while one configuration selecting several declarations is verified
and reviewed as a whole. CI then:

1. validates the required root files and pinned commit, including parsing
   `formalization.yaml` and enforcing Palomar's documented metadata minimum;
2. installs a matching `lean4export`;
3. runs [Comparator](https://github.com/leanprover/comparator) under its Landrun
   sandbox, using the three standard permitted axioms, and forces every exported
   proof through both Lean's kernel and the pinned independent NanoDa kernel;
4. computes the transitive source closure of `Challenge.lean`;
5. compiles the Challenge against frozen, canonical Mathlib/Tau Ceti output and
   verifies every transitive source byte;
6. publishes a machine-readable report as a run artifact

from other users, on pull requests or closed issues, and while the submission
is in any other state.
Eligibility is rechecked against the issue's current state when the command is
handled, so stale or duplicate requests do not start another verification.

The proof project may use arbitrary pinned **public GitHub** Git dependencies
at full 40-character commit SHAs. They build from source inside Palomar's fresh
Lake build directories. Submitted or substantive source repositories containing
Git submodules are rejected. An inert submodule gitlink in a dependency is
allowed because dependency submodules are never initialized or read; the exact
gitlink is retained by the archive fork. Git LFS pointers are rejected everywhere, as are
Git dependencies hosted anywhere other than GitHub. Palomar must be able to
preserve the complete source graph consumed by the accepted build in ordinary
Git. The Challenge is compiled
separately without candidate Lake configuration, against only verified
allowlisted dependencies; its protected module is the statement Comparator
exports. Common submitted prebuilt artifacts are rejected early, and no
candidate build output can replace the protected statement or frozen trusted
dependency modules. Only this statement surface is restricted; arbitrary pinned
dependencies remain available to the proof in `Solution.lean`. A Tau Ceti import
is recorded as a qualified trust surface; no other statement dependency is
accepted, including one from a project Palomar has already indexed.

`enable_nanoda` in a submitted `comparator.json` is retained for upstream
schema compatibility but is not authoritative. The trusted runner writes a
separate configuration with NanoDa enabled and passes that copy to Comparator.

AI review is not part of this repository's CI.
[`PalomarReviewer`](https://github.com/PalomarRegistry/PalomarReviewer) runs it
automatically against passing open issues, using the prompts in
[`PalomarPolicy`](https://github.com/PalomarRegistry/PalomarPolicy). No person starts a
review or approves its result.

## Required source layout

See [`PalomarRegistry/PalomarPolicy`](https://github.com/PalomarRegistry/PalomarPolicy/blob/main/CONTRIBUTING.md).
The root contract is:

```text
lean-toolchain
lakefile.toml
formalization.yaml
Challenge.lean
Solution.lean
comparator.json
LICENSE
```

The licence filename is case-insensitive and may instead use `LICENCE`,
`COPYING`, `UNLICENSE`, or `OFL`, with an optional `.md`, `.markdown`, or
`.txt` extension. Exactly one such regular root file is required. It must be
nonempty UTF-8 text, match one standard SPDX licence mechanically, and agree
exactly with the SPDX identifier in `project.license`.

`formalization.yaml` must be valid YAML with one top-level mapping and nonempty
project identity, authorship, license, classification, source citation,
automation-method, and review-status fields. Classification requires one or two
official arXiv subject classes and at least one MSC2020 code. The exact
mechanical minimum is documented in
[`PalomarPolicy/CONTRIBUTING.md`](https://github.com/PalomarRegistry/PalomarPolicy/blob/main/CONTRIBUTING.md#1-required-repository-shape).

The checked-in identifier lists under [`taxonomies/`](taxonomies/) are snapshots
of the official [arXiv taxonomy](https://arxiv.org/category_taxonomy) and
[MSC2020](https://msc2020.org/). Their purpose is exact intake validation; the
editorial AI separately checks whether the selected subjects are plausible for
the submitted result.

The prototype accepts public GitHub repositories and supported released or RC
Lean toolchains listed in [`toolchains.json`](toolchains.json).

## Licensing

PalomarSubmission's software is MIT-licensed. The vendored taxonomy data is a
separate work under the terms recorded in [`taxonomies/LICENSE.md`](taxonomies/LICENSE.md).
Submission licence validation covers the submitted repository snapshot only;
cited papers, reused formalizations, and dependencies retain their own licences.

## Security

Submission Lean is hostile input. The verification job has read-only repository
permissions and no credential in its environment; the issue-reporting job has a
separate token and never executes submitted data. See [`SECURITY.md`](SECURITY.md)
before changing the workflow or verifier.

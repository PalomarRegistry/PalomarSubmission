# Palomar Submission

Mechanical verification for the Palomar registry.

[**Submit a Lean-verified result →**](https://submit.palomar-registry.org)

The form asks for a public GitHub repository, an immutable commit, the
repository-relative path of exactly one Comparator configuration, and a
declaration of the submitter's relationship to the substantive formalization,
which is a claim about a person rather than about the code and is recorded
permanently. The repository itself carries the metadata. One configuration
becomes one Palomar entry; several configurations at the same repository and
commit are submitted separately, while one configuration selecting several
declarations is verified and reviewed as a whole. CI then:

1. validates the required root files and pinned commit, including parsing
   `formalization.yaml` and enforcing Palomar's documented metadata minimum;
2. installs a matching `lean4export`;
3. runs [Comparator](https://github.com/leanprover/comparator) under its Landrun
   sandbox, permitting at most the three standard axioms, and forces every
   exported proof through both Lean's kernel and the pinned independent NanoDa
   kernel;
4. compiles the Challenge against frozen, canonical Mathlib, Tau Ceti, or
   CSLib output;
5. computes the transitive source closure of the Challenge and verifies every
   byte in it;
6. publishes a machine-readable report as a run artifact.

Verification is dispatched by the submission server, not started from this
repository. The run carries the submission identifier in its name, and the
report leaves as an artifact rather than as a comment, so nothing here needs a
credential that can write anywhere.

The workflow has `preflight` and `full` modes. Both check out `main` and invoke
the same `verify_submission.py prepare` entry point; full verification merely
continues into the expensive toolchain and proof steps after preparation says
the submission is ready. Their run names and artifact names are distinct so a
preflight result cannot be mistaken for mechanical verification. Failed reports
carry a bounded diagnostics-v1 list: every item names its owner, explanation,
next action, retryability, optional location, and—only for fields in
[`formalization-profile.json`](formalization-profile.json)—whether a constrained
metadata repair may be offered.

Every report also labels its broad `phase` as `preparation` or `verification`.
That producer-owned classification lets downstream services keep preparation
failures actionable even when they are discovered during a full run.
[`browser-preflight-policy.json`](browser-preflight-policy.json) publishes the
bounded subset of preparation rules that the submission page can repeat at an
exact commit. It is advisory only: the workflow remains authoritative and
repeats every check after submission. Consumers must fail open when the policy
is unavailable or incompatible, and compare its complete contents before using
a browser result to ask for confirmation. The file is generated: it is what
`python -m scripts.browser_preflight_policy` projects from this repository's
constants, so change the constant and rerun that script with `--write` rather
than editing the document.

Formalization profile 4 reports every missing or invalid mechanically required
metadata field separately and includes only safely reusable values from a
recognized older shape. That lets the submission site explain and collect the
complete correction in one form. It never guesses classifications,
maintainers, source relationships, review claims, or whether a repository is a
thin wrapper; malformed or alias-bearing YAML remains a manual correction. The
profile carries the multiline `project.description` used as the public
registry abstract and structured `sources[].contributors` entries for
non-author source credits.

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
dependencies remain available to the proof in `Solution.lean`. Tau Ceti and
CSLib imports are recorded as qualified trust surfaces; no other statement
dependency is accepted, including one from a project Palomar has already
indexed.

NanoDa replay is a registry invariant, not a submitter option. The optional
`enable_nanoda` field in a submitted `comparator.json` is retained for upstream
compatibility but is deliberately non-authoritative: missing, false, or any
other JSON value does not disable or block verification. The trusted runner
writes a separate protected configuration with NanoDa enabled and passes that
copy to Comparator. This avoids making submitters maintain a switch whose value
Palomar must override for every accepted result.

AI review is not part of this repository's CI.
[`PalomarReviewer`](https://github.com/PalomarRegistry/PalomarReviewer) runs it
automatically against the mechanical report of a passing run, using the prompts
in [`PalomarPolicy`](https://github.com/PalomarRegistry/PalomarPolicy). No
person starts a review or approves its result.

## Required source layout

See [`PalomarRegistry/PalomarPolicy`](https://github.com/PalomarRegistry/PalomarPolicy/blob/main/CONTRIBUTING.md).
The contract is:

```text
lean-toolchain            # in the project or the repository root
lakefile.toml             #   or lakefile.lean, which also needs lake-manifest.json
formalization.yaml
<the Comparator configuration named by the submission>
<its challenge_module and solution_module sources>
LICENSE                   # repository root, and only there
```

Only `formalization.yaml` is required under that exact name. The Challenge and
Solution paths follow from `challenge_module` and `solution_module` in the
Comparator configuration, and the configuration's own path is the one the
submission named, so none of the three is a fixed filename. The selected project
need not be the repository root either: `project_path` may name a subdirectory,
and everything but the licence is resolved inside it.

The licence filename is case-insensitive and may instead use `LICENCE`,
`COPYING`, `UNLICENSE`, or `OFL`, with an optional `.md`, `.markdown`, or
`.txt` extension. Exactly one such regular root file is required. It must be
nonempty UTF-8 text, match one standard SPDX licence mechanically, and agree
exactly with the SPDX identifier in `project.license`.

Current metadata should declare `version: v0.4`; an omitted version follows the
upstream dispatcher's v0.4 default. `formalization.yaml` must be valid YAML with one top-level mapping and nonempty
project identity, description, authorship, license, classification, automation-method, and
review-status fields. Classification requires one to eight official arXiv subject
classes and permits up to eight distinct MSC2020 codes. Each project author may
be a name or a mapping with `name` and
optional `github` login and `orcid`; ORCIDs may use the bare identifier or the
canonical `https://orcid.org/` URL and are stored in bare form by the registry. The
current provenance
contract also requires a nonempty `project.responsible_maintainers` list and a
nonempty `sources` list. The submitted repository is the substantive proof
development by default, so ordinary submissions need no `repository` section.
A thin wrapper must instead provide a pinned
`repository.substantive_formalization`; the legacy explicit
`repository.role: thin-wrapper` spelling remains accepted. Every source,
including an `original-proof` entry, needs a
`relationship`. An `original-proof` entry must use `relationship: other`;
additional sources accompanying an original result may use `background` or
`other`. A `formalizes`, `adapts`, or `independently-proves` relationship instead
declares the result source-based and therefore conflicts with `type:
original-proof` anywhere in the list. Thus a result is original exactly when at
least one entry is `original-proof`, every such entry uses `other`, and every
other relationship is `background` or `other`; it is source-based exactly when
there is no `original-proof` entry and at least one relationship is
`formalizes`, `adapts`, or `independently-proves`. All other combinations fail.
A supplied source `type` is a concise free-text description such as `article`,
`paper`, `book`, `formalization`, or `web post`; it may be omitted except
where the exact value `original-proof` is the origin declaration. Invalid or
missing provenance fails mechanical verification with the field that needs
changing. Source `authors` are bibliographic authors. Optional
`contributors` records non-author credits as mappings with a nonempty `name`
and a free-form `role` of at most 200 characters.

`project.description` is the public registry abstract for the formalization as
a whole. It must be nonempty and should concisely identify the mathematical
content and principal results. The browser displays it during preliminary
checks, and an authenticated submitter may ask Palomar to open a pull request
changing this field before full verification starts.
New files should not add a top-level `provenance` block: put maintainers under
`project`, the optional thin-wrapper target under `repository`, and result origin
in the source entries as above. For compatibility with older files, the verifier
ignores a top-level `provenance` block and accepts singular
`project.responsible_maintainer` and `sources[].author` aliases; current plural
fields take precedence when both spellings are present. Result origin is always
derived from the current `sources` entries, so a `result_origin` value in an
ignored legacy block cannot override them. The exact mechanical minimum is
enforced here; the fields' intended meaning and authoring conventions are
documented in
[`PalomarPolicy/CONTRIBUTING.md`](https://github.com/PalomarRegistry/PalomarPolicy/blob/main/CONTRIBUTING.md#3-write-formalizationyaml).

The checked-in identifier lists under [`taxonomies/`](taxonomies/) are snapshots
of the official [arXiv taxonomy](https://arxiv.org/category_taxonomy) and
[MSC2020](https://msc2020.org/). Their purpose is exact intake validation; the
editorial AI separately checks whether the selected subjects are plausible for
the submitted result.

The prototype accepts public GitHub repositories and any released or RC Lean
toolchain at or above the minimum recorded in
[`toolchains.json`](toolchains.json). There is no list of accepted versions:
tooling revisions are derived from release tags, which is what a table of them
kept getting wrong. Rendering first selects the exact Verso release tag. When a
stable positive Lean patch release has no exact Verso tag, it uses that same
major/minor release line's patch-zero Verso tag and rebuilds the pinned source
with the submission's exact Lean toolchain. Release candidates never fall back,
and the resolved Verso commit is recorded in the render report. The file is
deliberately a closed record containing only its schema version and the minimum
Lean release; it does not claim to configure tooling repositories that the
verifier does not read from it.

## Licensing

PalomarSubmission's software is MIT-licensed. The vendored taxonomy data is a
separate work under the terms recorded in [`taxonomies/LICENSE.md`](taxonomies/LICENSE.md).
Submission licence validation covers the submitted repository snapshot only;
cited papers, reused formalizations, and dependencies retain their own licences.

## Security

Submission Lean is hostile input. The verification job has read-only repository
permissions and no credential in its environment, and its only output is a
bounded JSON artifact, so there is no second job holding a write token to
compromise. See [`SECURITY.md`](SECURITY.md) before changing the workflow or
verifier.

## Code boundaries

[`scripts/submission_contract.py`](scripts/submission_contract.py) owns the
dispatch-input allowlist and envelope validation, GitHub repository
normalization, and the complete `formalization.yaml` contract.
[`scripts/verify_submission.py`](scripts/verify_submission.py) applies the
remaining per-field dispatch rules for the commit, existing id, and
authorization while it orchestrates checkout and verification. It does not
carry a second request or metadata parser or a compatibility entry point.

## Development checks

Repository Python files use the `E`, `F`, `I`, `UP`, and `B` Ruff rule families declared
in [`pyproject.toml`](pyproject.toml). Install the single locked lint dependency
and run the same check as CI with:

```sh
python -m pip install --disable-pip-version-check --require-hashes \
  --no-deps --only-binary=:all: -r requirements-lint.txt
python -m ruff check .
```

The verifier runtime dependency remains separately locked in
[`requirements.txt`](requirements.txt); Ruff is not installed in verification
or cold-build jobs.

CI also rejects the wording of the retired intake mechanism, using the
stdlib-only checker in
[`.github/scripts/check_retired_intake_wording.py`](.github/scripts/check_retired_intake_wording.py).
It reads `SECURITY.md`, `README.md`, every Markdown file below `docs/`, and
every Python file below `scripts/`, including the ones sitting directly in it,
and it takes an optional repository root so the tests can run it against a
planted file. It rejects a closed set of collocations, not the bare word
"issue", which the tracker and private reporting still need. Anything it cannot
read is a failure rather than a file it passes over, because a scan surface
that quietly shrinks is invisible in a passing run. Run it with:

```sh
python .github/scripts/check_retired_intake_wording.py
```

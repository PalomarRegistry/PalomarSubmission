# Palomar Submission

Issue-based intake and mechanical verification for the Palomar registry.

[**Submit a Lean-verified result →**](https://github.com/kim-em/PalomarSubmission/issues/new?template=submit.yml)

The form asks only for a public GitHub repository and an immutable commit. The
repository itself carries the metadata and comparator configuration. CI then:

1. validates the required root files and pinned commit;
2. installs a matching `lean4export`;
3. runs [Comparator](https://github.com/leanprover/comparator) under its landrun
   sandbox, using the three standard permitted axioms;
4. computes the transitive source closure of `Challenge.lean`;
5. verifies that every challenge source comes from Lean core, Mathlib/Tau Ceti,
   or an exact repository commit already indexed in Palomar;
6. posts a machine-readable report and marks a passing issue
   `status:awaiting-review`.

The proof project may use arbitrary pinned Git dependencies. Only the trusted
statement surface—the transitive imports of `Challenge.lean`—is restricted.

AI review does not run in CI. An operator runs
[`PalomarReviewer`](https://github.com/kim-em/PalomarReviewer), which consumes
passing open issues using the prompts in
[`PalomarPolicy`](https://github.com/kim-em/PalomarPolicy).

## Required source layout

See [`kim-em/PalomarPolicy`](https://github.com/kim-em/PalomarPolicy/blob/main/CONTRIBUTING.md).
The root contract is:

```text
lean-toolchain
lakefile.toml
formalization.yaml
Challenge.lean
Solution.lean
comparator.json
```

The prototype accepts public GitHub repositories and supported released or RC
Lean toolchains listed in [`toolchains.json`](toolchains.json).

## Security

Submission Lean is hostile input. The verification job has read-only repository
permissions and no credential in its environment; the issue-reporting job has a
separate token and never executes submitted data. See [`SECURITY.md`](SECURITY.md)
before changing the workflow or verifier.

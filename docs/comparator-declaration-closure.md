# Comparator declaration-closure note

Date: 2026-09-23

Palomar judges with the `lake comparator` that ships in the submitted Lean
toolchain, at or above the floor `v4.35.0-rc2`; the mechanical report records
the lean4 commit that toolchain's release tag names. This note describes
`Lake/Check/Compare.lean`, `Lake/Check/Axioms.lean` and `Lake/CLI/Check.lean`
at that floor. A later toolchain is judged by its own copy of the same code.

`compareAt` first requires every configured theorem to have the same kind
(theorem or axiom) and the same statement in the exported Challenge and
Solution environments, and adds the constants used by each statement's type to
a worklist. Every configured definition must be a definition of the same type
and safety on both sides; its name enters the worklist. `Compare.loop` then
walks the used-constant graph transitively and requires each ordinary
declaration reached to be identical in both environments. The kernel's builtin
constants (`primitiveTargets`) seed the same walk, which is why Palomar exports
them alongside the declarations; the list is read from the toolchain's own
source rather than copied, and an export that lacked one would fail a valid
proof.

Configured `definition_names` are deliberate holes, not ordinary dependencies.
A named definition is compared by type and safety, the constants used by that
type are followed, and its body may differ. The axiom pass separately walks
the Solution proof and named definition bodies and rejects any axiom outside
`permitted_axioms`; the registered external kernels and Lean's own
`leanchecker` then replay the whole Solution export.

Configured `theorem_names` are holes in the same sense, and the walk treats
them so wherever it reaches them. A named theorem is compared by statement,
and its proof is not compared even when some other declaration's value
mentions it. A Challenge may therefore state a supporting lemma and leave its
proof to the Solution, provided the lemma is named in `theorem_names`; a
`sorry` on a declaration that is not named is still a mismatch. The axiom pass
and the kernel replays of the Solution are what establish that the Solution
proves each named statement.

Palomar never lets the comparator build or export. It builds the Solution
under its own sandbox, exports the canonical Challenge (compiled outside the
candidate Lake plan and published under a per-run alias) and the built
Solution with the toolchain's `leanexport`, and passes both files with
`--challenge-from-export` and `--solution-from-export`. In that form the
comparator skips dependency resolution and building, and it does not check
that the exports match the project: that is Palomar's responsibility, met by
producing both exports itself and snapshotting them before the judge runs.

Consequently, declarations used to determine a compared theorem's type do not
all need to be listed individually in `comparator.json`: the comparator follows
the relevant declaration closure mechanically. The rendered page may still show
only the declarations named in `theorem_names` and `definition_names`. Palomar
therefore labels that rendering as a partial “named compared declarations”
view and links to the full pinned `Challenge.lean`. The UI must not call the partial
render the complete statement without this disclosure.

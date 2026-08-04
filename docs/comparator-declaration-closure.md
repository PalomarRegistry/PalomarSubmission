# Comparator declaration-closure note

Date: 2026-08-01

Palomar pins Comparator commit
`68a064109f01c08f47c8edc9f51d6a2bbffaa188`. At that revision,
`Comparator.compareAt` first requires every configured theorem to have the same
kind and type in the exported Challenge and Solution environments. It adds all
constants used by those theorem types to a worklist. `Compare.loop` then walks
the used-constant graph transitively and requires each ordinary declaration to
be identical in both environments. The primitive constants selected in
`Main.lean` enter the same comparison.

Configured `definition_names` are deliberate holes, not ordinary dependencies.
Comparator requires a named hole to be a definition of the same kind and type
on both sides, follows the constants used by that type, and permits its body to
differ. The axiom pass separately traverses the Solution proof and named
definition bodies and rejects axioms outside `permitted_axioms`.

Consequently, declarations used to determine a compared theorem's type do not
all need to be listed individually in `comparator.json`: Comparator follows the
relevant declaration closure mechanically. The rendered page may still show
only the declarations named in `theorem_names` and `definition_names`. Palomar
therefore labels that rendering as a partial “named compared declarations”
view and links to the full pinned `Challenge.lean`. The UI must not call the partial
render the complete statement without this disclosure.

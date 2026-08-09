import Mathlib

/-!
# Module-identity cold regression

This source is intentionally not the root module `Challenge`. The renderer must
compile and render it as `PalomarCold.Challenge`, because Lean's generated name
for `modulePrivate` is sensitive to that module identity.
-/

namespace PalomarColdTauCetiFixture

private theorem modulePrivate : True := by
  trivial

theorem dependencyClosure : True := modulePrivate

end PalomarColdTauCetiFixture

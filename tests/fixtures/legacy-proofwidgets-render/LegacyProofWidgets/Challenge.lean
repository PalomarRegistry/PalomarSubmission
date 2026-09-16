import Mathlib

/-!
# Legacy ProofWidgets renderer regression

Mathlib v4.28 pins a ProofWidgets release whose JavaScript is supplied by its
Lake release archive rather than tracked in the source checkout.
-/

namespace LegacyProofWidgetsRenderFixture

private theorem modulePrivate : True := by
  trivial

theorem dependencyClosure : True := modulePrivate

end LegacyProofWidgetsRenderFixture

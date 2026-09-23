import Cslib

namespace PalomarColdFixture

/- Importing the root makes the candidate build reach cslib's broad module graph. -/
theorem dependencyClosure : True := by
  trivial

end PalomarColdFixture

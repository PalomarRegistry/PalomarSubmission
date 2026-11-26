# Licence detection supplement

Palomar detects a submission's licence with the pinned
[Licensee](https://github.com/licensee/licensee) gem. Licensee only recognises
the choosealicense.com corpus, which includes CC-BY-4.0 and CC-BY-SA-4.0 but no
earlier Creative Commons versions and no NonCommercial licences.
`scripts/detect_license.rb` adds the licences in this directory to that corpus,
so they are matched by the same 98% similarity test as every other licence.

`_licenses/` holds each licence's SPDX plain text under a short front matter
giving its SPDX identifier and SPDX name. `license-list-XML/` holds the
corresponding SPDX XML source files unchanged; Licensee uses their `<alt>`
markup to allow for permitted variation in length. Both come from the SPDX
License List ([license-list-data](https://github.com/spdx/license-list-data)
commit `cc4fe1ac462bd3ad3c81a07234baf7d9541ee871` and
[license-list-XML](https://github.com/spdx/license-list-XML) commit
`4103e93c4d1143310bd72879e51019e173cb5556`), which is published under
[CC BY 3.0](https://creativecommons.org/licenses/by/3.0/). The licence texts
themselves are published by Creative Commons.

Regenerate the directory with
`python3 scripts/update_licence_supplement.py LICENSE_LIST_DATA LICENSE_LIST_XML`,
which also lists the identifiers included and the reasons for the omissions.

"""Regenerate licence-supplement/ from checkouts of the SPDX licence list.

Licensee only recognises the choosealicense.com corpus. Palomar adds the SPDX
texts of the Creative Commons licences below so that the trusted detector can
recognise them with the same similarity test. Run this with checkouts of
https://github.com/spdx/license-list-data and
https://github.com/spdx/license-list-XML, then commit the result.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUPPLEMENT = ROOT / "licence-supplement"

# Version 1.0 is omitted: the SPDX texts of CC-BY-NC-1.0 and CC-BY-NC-SA-1.0
# are too similar for the detector to tell apart. NoDerivatives licences are
# omitted because Palomar does not accept them.
SUPPLEMENTARY_LICENCES = (
    "CC-BY-2.0",
    "CC-BY-2.5",
    "CC-BY-2.5-AU",
    "CC-BY-3.0",
    "CC-BY-3.0-AT",
    "CC-BY-3.0-AU",
    "CC-BY-3.0-DE",
    "CC-BY-3.0-IGO",
    "CC-BY-3.0-NL",
    "CC-BY-3.0-US",
    "CC-BY-SA-2.0",
    "CC-BY-SA-2.0-UK",
    "CC-BY-SA-2.1-JP",
    "CC-BY-SA-2.5",
    "CC-BY-SA-3.0",
    "CC-BY-SA-3.0-AT",
    "CC-BY-SA-3.0-DE",
    "CC-BY-SA-3.0-IGO",
    "CC-BY-NC-2.0",
    "CC-BY-NC-2.5",
    "CC-BY-NC-3.0",
    "CC-BY-NC-3.0-DE",
    "CC-BY-NC-3.0-IGO",
    "CC-BY-NC-4.0",
    "CC-BY-NC-SA-2.0",
    "CC-BY-NC-SA-2.0-DE",
    "CC-BY-NC-SA-2.0-FR",
    "CC-BY-NC-SA-2.0-UK",
    "CC-BY-NC-SA-2.5",
    "CC-BY-NC-SA-3.0",
    "CC-BY-NC-SA-3.0-DE",
    "CC-BY-NC-SA-3.0-IGO",
    "CC-BY-NC-SA-4.0",
)


def licence_file(identifier: str, title: str, text: str) -> str:
    """Return a licence in the choosealicense.com format that Licensee reads."""
    body = text.replace("\r\n", "\n").strip()
    return f"---\ntitle: {json.dumps(title)}\nspdx-id: {identifier}\n---\n\n{body}\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("license_list_data", type=Path)
    parser.add_argument("license_list_xml", type=Path)
    args = parser.parse_args()
    texts = SUPPLEMENT / "_licenses"
    xml = SUPPLEMENT / "license-list-XML"
    for directory in (texts, xml):
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)
    for identifier in SUPPLEMENTARY_LICENCES:
        details_path = args.license_list_data / "json" / "details" / f"{identifier}.json"
        details = json.loads(details_path.read_text(encoding="utf-8"))
        if details["licenseId"] != identifier or details["isDeprecatedLicenseId"]:
            raise SystemExit(f"{identifier} is not a current SPDX licence identifier")
        text = (args.license_list_data / "text" / f"{identifier}.txt").read_text(encoding="utf-8")
        (texts / f"{identifier.lower()}.txt").write_text(
            licence_file(identifier, details["name"], text), encoding="utf-8"
        )
        shutil.copyfile(
            args.license_list_xml / "src" / f"{identifier}.xml", xml / f"{identifier}.xml"
        )


if __name__ == "__main__":
    main()

"""Translate R License fields to SPDX expressions."""

import re
from dataclasses import dataclass

SPDX = {
    "AGPL-3": "AGPL-3.0-only",
    "AGPL (>= 3)": "AGPL-3.0-or-later",
    "Apache License 2.0": "Apache-2.0",
    "Apache License (== 2.0)": "Apache-2.0",
    "Apache License (>= 2)": "Apache-2.0",
    "Apache License (>= 2.0)": "Apache-2.0",
    "Artistic-1.0": "Artistic-1.0",
    "Artistic-2.0": "Artistic-2.0",
    "Artistic License 2.0": "Artistic-2.0",
    "BSD_2_clause": "BSD-2-Clause",
    "BSD_3_clause": "BSD-3-Clause",
    "BSD 3-clause License": "BSD-3-Clause",
    "BSL-1.0": "BSL-1.0",
    "CC0": "CC0-1.0",
    "CC BY 4.0": "CC-BY-4.0",
    "CC BY-NC 4.0": "CC-BY-NC-4.0",
    "CC BY-NC-ND 4.0": "CC-BY-NC-ND-4.0",
    "CC BY-NC-SA 4.0": "CC-BY-NC-SA-4.0",
    "CC BY-SA 4.0": "CC-BY-SA-4.0",
    "CPL": "CPL-1.0",
    "EPL": "EPL-1.0",
    "GPL": "GPL-2.0-or-later",
    "GPL-2": "GPL-2.0-only",
    "GPL (== 2)": "GPL-2.0-only",
    "GPL (>= 2)": "GPL-2.0-or-later",
    "GPL (>= 2.0)": "GPL-2.0-or-later",
    "GPL-3": "GPL-3.0-only",
    "GPL (>= 3)": "GPL-3.0-or-later",
    "GPL (>= 3.0)": "GPL-3.0-or-later",
    "LGPL": "LGPL-2.0-or-later",
    "LGPL-2": "LGPL-2.0-only",
    "LGPL (>= 2)": "LGPL-2.0-or-later",
    "LGPL (>= 2.0)": "LGPL-2.0-or-later",
    "LGPL-2.1": "LGPL-2.1-only",
    "LGPL (>= 2.1)": "LGPL-2.1-or-later",
    "LGPL-3": "LGPL-3.0-only",
    "LGPL (>= 3)": "LGPL-3.0-or-later",
    "LGPL (>= 3.0)": "LGPL-3.0-or-later",
    "MIT": "MIT",
    "MIT License": "MIT",
    "MPL-2.0": "MPL-2.0",
    "Mozilla Public License 2.0": "MPL-2.0",
}

# License texts shipped by r-base in lib/R/share/licenses.
R_LICENSE_FILES = {
    "Artistic-2.0": "Artistic-2.0",
    "GPL-2.0-only": "GPL-2", "GPL-2.0-or-later": "GPL-2",
    "GPL-3.0-only": "GPL-3", "GPL-3.0-or-later": "GPL-3",
    "LGPL-2.0-only": "LGPL-2", "LGPL-2.0-or-later": "LGPL-2",
    "LGPL-2.1-only": "LGPL-2.1", "LGPL-2.1-or-later": "LGPL-2.1",
    "LGPL-3.0-only": "LGPL-3", "LGPL-3.0-or-later": "LGPL-3",
}

_FILE = re.compile(r"^file\s+(LICEN[CS]E)$")
_WITH_FILE = re.compile(r"\s*\+\s*file\s+(LICEN[CS]E)$")
_OPERATORS = {">=": " ge ", "<=": " le ", "==": " eq ", ">": " gt ", "<": " lt "}


@dataclass(frozen=True)
class License:
    spdx: str
    r_files: tuple[str, ...]  # names under lib/R/share/licenses
    source_files: tuple[str, ...]  # LICENSE or LICENCE referenced by the field


def _normalize(term: str) -> str:
    term = " ".join(term.split())
    return re.sub(r"\s*\(\s*(>=|<=|==|>|<)\s*([^\s)]+)\s*\)", r" (\1 \2)", term)


def _license_ref(term: str) -> str:
    for operator, word in _OPERATORS.items():
        term = term.replace(operator, word)
    return "LicenseRef-" + (re.sub(r"[^A-Za-z0-9.]+", "-", term).strip("-") or "Unknown")


def convert(field: str) -> License:
    expressions: list[str] = []
    source_files: list[str] = []
    for term in field.split("|"):
        term = _normalize(term)
        match = _FILE.match(term)
        if match:
            source_files.append(match.group(1))
            continue
        match = _WITH_FILE.search(term)
        if match:
            source_files.append(match.group(1))
            term = term[: match.start()]
        if term:
            expressions.append(SPDX.get(term) or _license_ref(term))
    if not expressions:
        expressions.append(_license_ref("file-LICENSE" if source_files else ""))
    expressions = list(dict.fromkeys(expressions))
    r_files = dict.fromkeys(R_LICENSE_FILES[e] for e in expressions if e in R_LICENSE_FILES)
    return License(" OR ".join(expressions), tuple(r_files), tuple(dict.fromkeys(source_files)))

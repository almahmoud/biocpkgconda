import unittest

from biocpkgconda import licenses


class ConvertTest(unittest.TestCase):
    def check(self, field, spdx, r_files=(), source_files=()):
        result = licenses.convert(field)
        self.assertEqual((result.spdx, result.r_files, result.source_files), (spdx, r_files, source_files), field)

    def test_common_licenses(self):
        self.check("Artistic-2.0", "Artistic-2.0", ("Artistic-2.0",))
        self.check("GPL-2", "GPL-2.0-only", ("GPL-2",))
        self.check("GPL-3", "GPL-3.0-only", ("GPL-3",))
        self.check("GPL (>= 2)", "GPL-2.0-or-later", ("GPL-2",))
        self.check("GPL (>= 2.0)", "GPL-2.0-or-later", ("GPL-2",))
        self.check("GPL (>= 3)", "GPL-3.0-or-later", ("GPL-3",))
        self.check("GPL", "GPL-2.0-or-later", ("GPL-2",))
        self.check("LGPL", "LGPL-2.0-or-later", ("LGPL-2",))
        self.check("LGPL-2.1", "LGPL-2.1-only", ("LGPL-2.1",))
        self.check("LGPL (>= 3)", "LGPL-3.0-or-later", ("LGPL-3",))
        self.check("AGPL-3", "AGPL-3.0-only")
        self.check("CC0", "CC0-1.0")
        self.check("CC BY 4.0", "CC-BY-4.0")
        self.check("CC BY-SA 4.0", "CC-BY-SA-4.0")
        self.check("Apache License (== 2.0)", "Apache-2.0")
        self.check("EPL", "EPL-1.0")
        self.check("MPL-2.0", "MPL-2.0")

    def test_license_files(self):
        self.check("MIT + file LICENSE", "MIT", source_files=("LICENSE",))
        self.check("MIT + file LICENCE", "MIT", source_files=("LICENCE",))
        self.check("BSD_3_clause + file LICENSE", "BSD-3-Clause", source_files=("LICENSE",))
        self.check("BSD_2_clause + file LICENSE", "BSD-2-Clause", source_files=("LICENSE",))
        self.check("GPL-3 | file LICENSE", "GPL-3.0-only", ("GPL-3",), ("LICENSE",))
        self.check("file LICENSE", "LicenseRef-file-LICENSE", source_files=("LICENSE",))

    def test_alternatives(self):
        self.check("GPL-2 | GPL-3", "GPL-2.0-only OR GPL-3.0-only", ("GPL-2", "GPL-3"))
        self.check("Artistic-2.0 | GPL-2 + file LICENSE", "Artistic-2.0 OR GPL-2.0-only",
                   ("Artistic-2.0", "GPL-2"), ("LICENSE",))
        self.check("GPL (>= 2) | GPL (>= 2.0)", "GPL-2.0-or-later", ("GPL-2",))

    def test_whitespace_variants(self):
        self.check("GPL(>=2)", "GPL-2.0-or-later", ("GPL-2",))
        self.check("LGPL (>=\n 2.1)", "LGPL-2.1-or-later", ("LGPL-2.1",))

    def test_unmapped(self):
        self.check("Unlimited", "LicenseRef-Unlimited")
        self.check("GPL (> 2)", "LicenseRef-GPL-gt-2")
        self.check("CeCILL | file LICENSE", "LicenseRef-CeCILL", source_files=("LICENSE",))
        self.check("", "LicenseRef-Unknown")

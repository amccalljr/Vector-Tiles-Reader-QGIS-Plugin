# -*- coding: utf-8 -*-
#
# This code is licensed under the GPL 2.0 license.
#
import unittest
import coverage
import os
import sys
from qgis.core import *
from qgis.utils import iface
from PyQt4.QtCore import *

from test_mbtiles_source import MbtileSourceTests
from test_tilehelper import TileHelperTests
from test_vtreader import IfaceTests
from test_tilejson import TileJsonTests


def suites():
    return [
        unittest.makeSuite(MbtileSourceTests),
        unittest.makeSuite(TileHelperTests),
        unittest.makeSuite(IfaceTests),
        unittest.makeSuite(TileJsonTests),
    ]


# run all tests using unittest skipping nose or testplugin
def run_all():
    cov = coverage.Coverage()
    cov.start()
    for s in suites():
        print("----------------------------------------------------------------------")
        print("")
        unittest.TextTestRunner(verbosity=3, stream=sys.stdout).run(s)
        print("")
    cov.stop()
    cov.save()
    cov.html_report(directory='tests/htmlcov')
    print cov.report()


if __name__ == "__main__":
    run_all()

#!/usr/bin/env python2
# vim:fileencoding=utf-8
# License: GPLv3 Copyright: 2016, Kovid Goyal <kovid at kovidgoyal.net>

from __future__ import (unicode_literals, division, absolute_import,
                        print_function)

from bypy.constants import CFLAGS, ismacos, iswindows
from bypy.utils import simple_build, ModifiedEnv, copy_headers


def main(args):
    if iswindows:
        # On windows we dont actually build sqlite as the python build script
        # downloads its own version locked sqlite. We just install the headers
        copy_headers('sqlite3*.h')
        return
    cflags = CFLAGS
    if ismacos:
        cflags += ' -O2 -DSQLITE_ENABLE_LOCKING_STYLE'
    with ModifiedEnv(CFLAGS=cflags):
        simple_build('--disable-dependency-tracking --disable-static')

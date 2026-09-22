# SPDX-License-Identifier: AGPL-3.0-only
# boot.py runs before main.py at every start.
#
# The content is the unchanged MicroPython template: everything is commented, nothing
# happens here. The file is still versioned because it is on the board and a newly
# flashed device would otherwise not have the same status as a grown one.
#
# Why nothing belongs in it: boot.py is also compiled and needs heap, then main.py and
# the radio parts.
#
# webrepl consciously: it haunts the net unprotected.

# This file is executed on every boot (including wake-boot from deepsleep)
#import esp
#esp.osdebug(None)
#import webrepl
#webrepl.start()

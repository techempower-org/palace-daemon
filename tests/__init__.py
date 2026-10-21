
# Many tests read a spawned mine's target from a fixed argv position. In
# production main._low_priority prefixes that argv with ionice/nice; it is
# off for the suite, and tests/test_mine_low_priority.py turns it on itself.
import os as _os

_os.environ.setdefault("PALACE_MINE_LOW_PRIORITY", "0")

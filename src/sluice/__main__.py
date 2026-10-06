"""``python -m sluice <command>``: the same CLI without installing the console script."""

import sys

from sluice.cli import main

sys.exit(main())

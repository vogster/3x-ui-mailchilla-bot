"""
The tests, when they are run as a package — `python -m unittest tests.test_tariffs`.

`python -m unittest discover -s tests`, which is what CI runs, never imports
this file: there the directory is the top of the tree, and each module brings
the sandbox in for itself. Run as a package, this comes first, and puts the
directory on the path so that the same `import sandbox` works from inside it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sandbox  # noqa: E402,F401

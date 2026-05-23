"""PyInstaller runtime hook for gi (PyGObject) on Linux.

The gi Python package (including _gi C extension and overrides) is now
bundled.  This hook adds the system Python paths so that any distro-specific
gi overrides or other system-installed packages are still importable.

Typelibs (.typelib files in /usr/lib/girepository-1.0/) are loaded at
runtime by libgirepository and are never bundled.

Required system packages (Ubuntu/Debian):
    sudo apt install python3-gi python3-gi-cairo \\
                     gir1.2-gtk-3.0 gir1.2-webkit2-4.1

Other distros install the equivalent packages.
"""

import sys
import os

for _candidate in (
    "/usr/lib/python3/dist-packages",
    f"/usr/lib/python3.{sys.version_info.minor}/site-packages",
    f"/usr/lib64/python3.{sys.version_info.minor}/site-packages",
):
    if os.path.isdir(_candidate) and _candidate not in sys.path:
        sys.path.append(_candidate)

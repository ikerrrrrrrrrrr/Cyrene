"""PyInstaller runtime hook for gi (PyGObject) on Linux.

We exclude 'gi' from the frozen bundle because its typelibs reference
system shared libraries (GTK, WebKit2GTK) that differ across distros.
This hook adds the system gi installation path to sys.path so the
frozen Python interpreter can find the host's PyGObject at runtime.

Required system packages (Ubuntu/Debian):
    sudo apt install python3-gi python3-gi-cairo \\
                     gir1.2-gtk-3.0 gir1.2-webkit2-4.1

Other distros install the equivalent packages.
"""

import sys
import os

for _candidate in (
    "/usr/lib/python3/dist-packages",
    "/usr/lib/python3.12/site-packages",
    "/usr/lib64/python3.12/site-packages",
):
    if os.path.isdir(_candidate) and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

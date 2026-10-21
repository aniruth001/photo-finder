"""Photo Finder Studio: multi-tenant version (photography companies, clients, events, share links)."""
import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:  # faces.py lives next to this package
    sys.path.insert(0, _ROOT)

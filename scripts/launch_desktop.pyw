"""Terminal-free source entry point; use the configured local shortcut."""
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root / 'app/src'), str(root)]

from flightmill.desktop.bootstrap import main  # noqa: E402

main()

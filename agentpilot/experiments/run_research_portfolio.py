"""Source-checkout entrypoint for the installed portfolio research command."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
for source in reversed((ROOT / 'agentpilot/src', ROOT / 'agentpilot/integrations/arena/src', ROOT / 'arena/src')):
    sys.path.insert(0, str(source))

from agentpilot_arena.research import main

if __name__ == '__main__':
    raise SystemExit(main())

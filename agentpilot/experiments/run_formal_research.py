"""Run formal AIME/Terminal-Bench research from a source checkout."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
for name in reversed(('agentpilot/src', 'agentpilot/integrations/arena/src', 'arena/src')):
    sys.path.insert(0, str(ROOT / name))

from agentpilot_arena.formal import main

if __name__ == '__main__':
    raise SystemExit(main())

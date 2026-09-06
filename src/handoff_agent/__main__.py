"""Allow running as: python -m handoff_agent"""

import sys

from handoff_agent.cli import main

if __name__ == "__main__":
    sys.exit(main())

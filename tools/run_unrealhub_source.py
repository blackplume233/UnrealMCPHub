import os
import sys


ROOT = r"C:\Users\alain\Documents\Playground\UnrealMCPHub"
SRC = os.path.join(ROOT, "src")

if SRC not in sys.path:
    sys.path.insert(0, SRC)

from unrealhub.cli import main


if __name__ == "__main__":
    main()

"""Allow `python -m northcua.cli` after the CLI package split."""

from .main import main

if __name__ == "__main__":
    main()

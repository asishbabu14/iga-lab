"""Allow `python -m iga_lab` to run the demo."""

from iga_lab.demo import main

if __name__ == "__main__":
    raise SystemExit(main())

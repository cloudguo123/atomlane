"""Read-only I/O fixture that receives advice but no automatic rewrite."""

from pathlib import Path


def read_one(path):
    return Path(path).read_text()


def main(paths):
    return [read_one(path) for path in paths]


if __name__ == "__main__":
    main([])

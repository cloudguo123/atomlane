"""Native-library fixture that should prefer the semantic owner."""

import numpy as np


def transform(value):
    return np.sin(value)


def main(values):
    return [transform(value) for value in values]


if __name__ == "__main__":
    main([])

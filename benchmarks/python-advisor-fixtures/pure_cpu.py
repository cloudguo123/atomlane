"""Eligible ordered CPU map used by the public advisor evidence report."""


def transform(value):
    return (value * value + 17) % 997


def main():
    values = list(range(128))
    results = [transform(value) for value in values]
    return results


if __name__ == "__main__":
    main()

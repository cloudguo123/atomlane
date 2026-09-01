"""Unsafe shared-state fixture that must fail closed."""

total = 0


def accumulate(value):
    global total
    total += value
    return total


def main():
    return [accumulate(value) for value in range(16)]


if __name__ == "__main__":
    main()

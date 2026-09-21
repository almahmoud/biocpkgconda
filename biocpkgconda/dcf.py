"""Debian control file (DCF) parsing for R PACKAGES and DESCRIPTION files."""


def parse(text: str) -> list[dict[str, str]]:
    """Return one dict per record. Continuation lines are joined with newlines."""
    records: list[dict[str, str]] = []
    record: dict[str, str] = {}
    key = None
    for line in text.splitlines():
        if not line.strip():
            if record:
                records.append(record)
            record, key = {}, None
        elif line[0] in " \t":
            if key is None:
                raise ValueError(f"continuation line without a field: {line!r}")
            record[key] = f"{record[key]}\n{line.strip()}".lstrip("\n")
        else:
            key, sep, value = line.partition(":")
            if not sep or not key.strip():
                raise ValueError(f"malformed line: {line!r}")
            key = key.strip()
            record[key] = value.strip()
    if record:
        records.append(record)
    return records


def parse_one(text: str) -> dict[str, str]:
    records = parse(text)
    if len(records) != 1:
        raise ValueError(f"expected one record, found {len(records)}")
    return records[0]

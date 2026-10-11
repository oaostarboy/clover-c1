"""Order export."""


def to_csv(rows, fields):
    """CSV text: a header line, then one line per row, in order."""
    lines = [",".join(fields)]
    for row in rows:
        lines.append(",".join(str(row[field]) for field in fields))
    return "\n".join(lines) + "\n"

from reports.export import to_csv

FIELDS = ["id", "total"]


def test_every_row_is_exported():
    rows = [{"id": 1, "total": 5}, {"id": 2, "total": 7}, {"id": 3, "total": 9}]
    assert to_csv(rows, FIELDS) == "id,total\n1,5\n2,7\n3,9\n"


def test_single_row_is_exported():
    assert to_csv([{"id": 1, "total": 5}], FIELDS) == "id,total\n1,5\n"


def test_no_rows_gives_header_only():
    assert to_csv([], FIELDS) == "id,total\n"

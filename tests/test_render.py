import subprocess
import sys

import pytest


@pytest.mark.parametrize("columns", ["", "abc", "0", "120"])
def test_render_import_tolerates_any_columns_value(columns):
    # COLUMNS="" used to raise ValueError at import time, breaking every wactx command.
    proc = subprocess.run(
        [sys.executable, "-c", "import wactx.render as r; print(r.console.width)"],
        env={"COLUMNS": columns, "PATH": ""},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    if columns == "120":
        assert proc.stdout.strip() == "120"

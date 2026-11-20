"""Power-meter verification platform.

Reports are the historical record: report number (monotonically increasing in
*verification* order, but uploads may arrive out of order), an inclusive serial
range ``[serial_first, serial_last]``, and a list of failed serials (empty means
the whole range passed).

The ruling for any serial number comes from the *highest-numbered* non-retracted
report whose range covers it.  Rulings are evaluated whenever needed from the
report table, never materialised per serial.  All ranges are half-open integer
intervals ``[lo, hi)`` internally so that a span of a million serials is one row.
"""

from .db import connect, init_db  # noqa: F401
from .service import (  # noqa: F401
    ConflictError,
    ReportInput,
    ServiceError,
    ValidationError,
    create_report,
    get_overview,
    query_range,
)

__version__ = "1.0.0"

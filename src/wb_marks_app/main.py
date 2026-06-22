from __future__ import annotations

import uvicorn

from wb_marks_app.webapp import create_app


def main() -> int:
    uvicorn.run(create_app(), host="0.0.0.0", port=8000)
    return 0

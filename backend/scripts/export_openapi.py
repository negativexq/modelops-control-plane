"""Writes the control plane's OpenAPI schema to a file (default:
frontend/openapi.json), the input the dashboard's TypeScript types are generated
from (`npm run gen:api` in frontend/). CI regenerates both and fails on any diff,
so an API change can't ship without the dashboard's types following it.

    python -m scripts.export_openapi [output_path]
"""

import json
import sys
from pathlib import Path

from app.main import app

DEFAULT_OUTPUT = Path(__file__).resolve().parents[2] / "frontend" / "openapi.json"


def main() -> None:
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT
    schema = app.openapi()
    output.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
    print(f"wrote {output} ({len(schema['paths'])} paths)")


if __name__ == "__main__":
    main()

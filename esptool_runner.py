import sys

import esptool


if __name__ == "__main__":
    try:
        result = esptool.main(sys.argv[1:])
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        raise SystemExit(code)
    raise SystemExit(result if isinstance(result, int) else 0)

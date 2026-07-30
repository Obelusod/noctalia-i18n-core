"""Verify an installed distribution without using the source checkout."""

from importlib.metadata import version
from importlib.resources import files

import noctalia_i18n_core


def main() -> None:
    required = {
        "Delivery",
        "Monitor",
        "NoctaliaSource",
        "MonitorResult",
        "Route",
        "SQLiteState",
    }
    missing = required.difference(noctalia_i18n_core.__all__)
    if missing:
        raise RuntimeError(f"Distribution is missing public API: {sorted(missing)}")
    if not files("noctalia_i18n_core").joinpath("py.typed").is_file():
        raise RuntimeError("Distribution is missing py.typed")
    print(f"noctalia-i18n-core {version('noctalia-i18n-core')} is importable")


if __name__ == "__main__":
    main()

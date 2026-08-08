"""Verify the installed public package surface."""

from importlib.metadata import version
from importlib.resources import files

import noctalia_i18n_core


def main() -> None:
    required = {
        "Change",
        "ChangeAction",
        "Locale",
        "NoctaliaClient",
        "Project",
        "Review",
        "SOURCE_LOCALE",
    }
    if set(noctalia_i18n_core.__all__) != required:
        raise RuntimeError("The public package API is not declared consistently")
    missing = tuple(name for name in required if not hasattr(noctalia_i18n_core, name))
    if missing:
        raise RuntimeError(f"The public package API is incomplete: {missing!r}")
    methods = (
        "catalog",
        "changes",
        "close",
        "iter_changes",
        "keys",
        "missing_keys",
        "project",
        "reviews",
        "search_translations",
        "translations",
    )
    missing_methods = tuple(
        name
        for name in methods
        if not callable(getattr(noctalia_i18n_core.NoctaliaClient, name, None))
    )
    if missing_methods:
        raise RuntimeError(f"The client API is incomplete: {missing_methods!r}")
    if not files("noctalia_i18n_core").joinpath("py.typed").is_file():
        raise RuntimeError("Distribution is missing py.typed")
    print(f"noctalia-i18n-core {version('noctalia-i18n-core')} is importable")


if __name__ == "__main__":
    main()

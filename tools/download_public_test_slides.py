from pathlib import Path
from urllib.request import urlretrieve


ROOT = Path(__file__).resolve().parent.parent
TESTDATA = ROOT / "tests" / "testdata"
OPENSLIDE_SMALL = TESTDATA / "CMU-1-Small-Region.svs"
OPENSLIDE_URL = "https://openslide.cs.cmu.edu/download/openslide-testdata/Aperio/CMU-1-Small-Region.svs"


def ensure_dir() -> None:
    TESTDATA.mkdir(parents=True, exist_ok=True)


def ensure_openslide_small() -> None:
    if OPENSLIDE_SMALL.exists():
        return
    urlretrieve(OPENSLIDE_URL, OPENSLIDE_SMALL)


def main() -> None:
    ensure_dir()
    ensure_openslide_small()
    print(f"Prepared test slides in {TESTDATA}")


if __name__ == "__main__":
    main()

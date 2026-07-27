from pathlib import Path
from shutil import copyfile
from urllib.request import urlretrieve


ROOT = Path(__file__).resolve().parent.parent
TESTDATA = ROOT / "tests" / "testdata"
OPENSLIDE_SMALL = TESTDATA / "CMU-1-Small-Region.svs"
MUSSEL_SMALL = TESTDATA / "948176.svs"
MUSSEL_LOCAL = ROOT.parent / "mussel" / "tests" / "testdata" / "948176.svs"
OPENSLIDE_URL = "https://openslide.cs.cmu.edu/download/openslide-testdata/Aperio/CMU-1-Small-Region.svs"
MUSSEL_URL = "https://raw.githubusercontent.com/pathology-data-mining/Mussel/master/tests/testdata/948176.svs"


def ensure_dir() -> None:
    TESTDATA.mkdir(parents=True, exist_ok=True)


def ensure_openslide_small() -> None:
    if OPENSLIDE_SMALL.exists():
        return
    urlretrieve(OPENSLIDE_URL, OPENSLIDE_SMALL)


def ensure_mussel_small() -> None:
    if MUSSEL_SMALL.exists():
        return
    if MUSSEL_LOCAL.exists():
        copyfile(MUSSEL_LOCAL, MUSSEL_SMALL)
        return
    urlretrieve(MUSSEL_URL, MUSSEL_SMALL)


def main() -> None:
    ensure_dir()
    ensure_openslide_small()
    ensure_mussel_small()
    print(f"Prepared test slides in {TESTDATA}")


if __name__ == "__main__":
    main()

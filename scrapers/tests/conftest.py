"""Put the scrapers package on sys.path so tests import it the way the
entrypoints do (``import normalize``, ``from adapters... import``)."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

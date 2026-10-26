import struct
import zlib

import pytest
from fastapi.testclient import TestClient

from app.ai import MockAgent
from app.config import Settings
from app.main import create_app
from app.paypal import MockPayPal


def make_png(seed: int = 0, size: int = 8) -> bytes:
    """Tiny valid PNG; different seeds -> different bytes/hashes."""
    # each row: filter byte + size*3 bytes
    rows = b""
    for y in range(size):
        rows += b"\x00" + b"".join(bytes(((x * 31 + y * 17 + seed) % 256, 200, 180)) for x in range(size))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


@pytest.fixture
def settings(tmp_path):
    return Settings(
        gemini_api_key="", paypal_client_id="", paypal_client_secret="", mode="mock",
        approval_threshold=0.75, database_path=str(tmp_path / "t.db"), upload_dir=str(tmp_path / "up"),
    )


@pytest.fixture
def gateway():
    return MockPayPal()


@pytest.fixture
def client(settings, gateway):
    app = create_app(settings, agent=MockAgent(), gateway=gateway)
    return TestClient(app)


@pytest.fixture
def png():
    return make_png

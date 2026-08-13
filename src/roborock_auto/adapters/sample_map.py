"""Bundled sample map PNG for FakeRobot and the live map fallback (base64-embedded, no asset files)."""
import base64

_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAGQAAABkCAIAAAD/gAIDAAAAzUlEQVR42u3doQ2AMBRF0U7CJNUIRmCI6gqGQRImQTMRFkUwpL/h"
    "vNwJzgIv5TzqZQkBrI+xpmWLU11LnGDBggULFixYsGDBggULFixYsGDBggULFixYsGDBggULFixYsGDBggULFixYsGDBggULFixY"
    "sGDBggULVudYxz7HabjtrKVtsGD9HKu5VAdYEYxgwYIFCxYsWLBgwYIFCxYsWLBgwYIFCxYsWLBgwYIFCxYsWLBgwYIFCxYsWLBg"
    "wYIFCxYsWLBgPWHJvyGsRl0YuSBU1QyikgAAAABJRU5ErkJggg=="
)

SAMPLE_MAP_PNG: bytes = base64.b64decode(_B64)

from __future__ import annotations

import asyncio
import base64
import io
import json
import sys
import unittest
from unittest.mock import patch

from neuro_voice.vision.obs import (
    ObsCapture,
    ObsRequestError,
    ObsWebSocketClient,
    build_authentication,
    decode_image_data,
)
from neuro_voice.vision.video import VideoObservationService


class ObsCaptureTests(unittest.TestCase):
    def _jpeg(self) -> bytes:
        from PIL import Image

        out = io.BytesIO()
        Image.new("RGB", (80, 45), (20, 80, 140)).save(out, format="JPEG")
        return out.getvalue()

    def test_authentication_matches_obs_websocket_protocol_example(self):
        value = build_authentication(
            "supersecretpassword",
            "lM1GncleQOaCu9lT1yeUZhFYnqhsLLP1G5lAGo3ixaI=",
            "+IxH4CnCiqpX1rM9scsNynZzbOe4KhDeYcTNS3PDaeY=",
        )
        # Fixed vector for the v5 two-stage SHA256/base64 algorithm.  The
        # protocol document's later Identify JSON is illustrative rather than
        # the output of its preceding password/salt/challenge sample.
        self.assertEqual(value, "1Ct943GAT+6YQUUX47Ia/ncufilbe6+oD6lY+5kaCu4=")

    def test_decode_source_screenshot_data_uri(self):
        raw = self._jpeg()
        uri = "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
        self.assertEqual(decode_image_data(uri), raw)

    def test_wire_client_identifies_and_matches_request_id(self):
        class Socket:
            def __init__(self):
                self.sent = []
                self.recv_count = 0

            async def send(self, value):
                self.sent.append(json.loads(value))

            async def recv(self):
                self.recv_count += 1
                if self.recv_count == 1:
                    return json.dumps({"op": 0, "d": {
                        "obsStudioVersion": "32.0.0", "obsWebSocketVersion": "5.6.0",
                        "rpcVersion": 1,
                    }})
                if self.recv_count == 2:
                    return json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}})
                request = self.sent[-1]["d"]
                return json.dumps({"op": 7, "d": {
                    "requestType": request["requestType"],
                    "requestId": request["requestId"],
                    "requestStatus": {"result": True, "code": 100},
                    "responseData": {"obsVersion": "32.0.0"},
                }})

            async def close(self):
                return None

        socket = Socket()

        class WebSockets:
            @staticmethod
            async def connect(*_args, **_kwargs):
                return socket

        async def run():
            client = ObsWebSocketClient(timeout=1)
            try:
                return await client.request("GetVersion")
            finally:
                await client.close()

        with patch.dict(sys.modules, {"websockets": WebSockets}):
            result = asyncio.run(run())
        self.assertEqual(result["obsVersion"], "32.0.0")
        self.assertEqual(socket.sent[0]["op"], 1)
        self.assertEqual(socket.sent[1]["d"]["requestType"], "GetVersion")

    def test_capture_requests_active_fresh_obs_source(self):
        raw = self._jpeg()

        class Client:
            def __init__(self):
                self.requests = []

            async def request(self, name, data=None):
                self.requests.append((name, data))
                if name == "GetSourceActive":
                    return {"videoActive": True, "videoShowing": True}
                return {"imageData": "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")}

        capture = ObsCapture("127.0.0.1", 4455, "", "Minecraft", max_width=640)
        capture.client = Client()
        frame = asyncio.run(capture.capture_media())
        self.assertEqual([item[0] for item in capture.client.requests], [
            "GetSourceActive", "GetSourceScreenshot",
        ])
        self.assertEqual(frame.metadata["source"], "obs")
        self.assertEqual(frame.metadata["capture_backend"], "obs-websocket")
        self.assertTrue(frame.metadata["fresh_request"])
        self.assertIn("frame_capture_ms", frame.metadata)
        self.assertIn("frame_encode_ms", frame.metadata)

    def test_inactive_source_is_not_replaced_with_desktop(self):
        class Config:
            def get(self, key, default=None):
                return {
                    "video.input": "obs", "video.max_queue_size": 8,
                }.get(key, default)

            def section(self, _name):
                return {}

        class Capture:
            async def capture_media(self):
                raise ObsRequestError("OBS source inactive")

        service = VideoObservationService(Config(), object())
        service._capture = Capture()
        frame = asyncio.run(service.capture_current_frame())
        self.assertIsNone(frame)
        self.assertEqual(service.input_kind, "obs")
        self.assertIn("inactive", service.last_capture_error)
        self.assertFalse(service.is_game_fallback)


if __name__ == "__main__":
    unittest.main()

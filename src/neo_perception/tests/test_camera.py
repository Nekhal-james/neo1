"""Finding the camera on a Pi, where most of /dev/video* is not a camera."""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from neo_perception import camera as cam


def _sysfs(root, nodes: dict[int, str]):
    for index, name in nodes.items():
        (root / f"video{index}").mkdir(parents=True)
        (root / f"video{index}" / "name").write_text(name + "\n")
    return root


PI_WITH_WEBCAM = {
    0: "USB Camera: USB Camera",
    1: "USB Camera: USB Camera",
    10: "bcm2835-codec-decode",
    13: "bcm2835-isp-output0",
    19: "rpivid",
}


class FakeCapture:
    def __init__(self, target, behaviour):
        self.target = target
        self.behaviour = behaviour.get(target, "closed")
        self.released = False
        self.reads = 0

    def isOpened(self):
        return self.behaviour != "closed"

    def set(self, *_):
        return True

    def read(self):
        self.reads += 1
        if self.behaviour == "frames" or (self.behaviour == "one-frame" and self.reads == 1):
            return True, np.zeros((480, 640, 3), np.uint8)
        return False, None

    def release(self):
        self.released = True


@pytest.fixture
def fake_cv2(monkeypatch):
    behaviour: dict = {}
    opened: list[FakeCapture] = []
    module = types.ModuleType("cv2")
    module.CAP_PROP_FOURCC = 6
    module.CAP_PROP_FRAME_WIDTH = 3
    module.CAP_PROP_FRAME_HEIGHT = 4
    module.CAP_PROP_FPS = 5
    module.CAP_PROP_BUFFERSIZE = 38
    module.VideoWriter_fourcc = lambda *c: 0
    module.VideoCapture = lambda target: opened.append(FakeCapture(target, behaviour)) or opened[-1]
    monkeypatch.setitem(sys.modules, "cv2", module)
    return SimpleCv2(behaviour, opened)


class SimpleCv2:
    def __init__(self, behaviour, opened):
        self.behaviour = behaviour
        self.opened = opened


def test_real_cameras_are_tried_before_codec_nodes(tmp_path, monkeypatch):
    monkeypatch.setattr(cam, "V4L_SYSFS", _sysfs(tmp_path, PI_WITH_WEBCAM))
    devices = cam.list_devices()
    assert [d.path for d in devices[:2]] == ["/dev/video0", "/dev/video1"]
    assert all(not d.likely for d in devices[2:])


def test_no_video4linux_at_all_is_no_devices(tmp_path, monkeypatch):
    monkeypatch.setattr(cam, "V4L_SYSFS", tmp_path / "absent")
    assert cam.list_devices() == []
    assert not cam.Camera().available()[0]


def test_a_node_that_opens_but_never_delivers_is_passed_over(tmp_path, monkeypatch, fake_cv2):
    monkeypatch.setattr(cam, "V4L_SYSFS", _sysfs(tmp_path, PI_WITH_WEBCAM))
    fake_cv2.behaviour.update({"/dev/video0": "silent", "/dev/video1": "frames"})
    camera = cam.Camera(retry_s=0)
    assert camera.open()
    assert camera.device_path == "/dev/video1"
    assert fake_cv2.opened[0].released, "the silent node is not left held open"


def test_a_camera_that_stops_is_released_and_found_again(tmp_path, monkeypatch, fake_cv2):
    monkeypatch.setattr(cam, "V4L_SYSFS", _sysfs(tmp_path, {0: "USB Camera"}))
    fake_cv2.behaviour["/dev/video0"] = "one-frame"
    camera = cam.Camera(retry_s=0)
    assert camera.open()
    assert camera.read() is None, "the open consumed the only frame"
    assert camera.device_path is None and "unplugged" in camera.error

    fake_cv2.behaviour["/dev/video0"] = "frames"
    assert camera.read() is not None
    assert camera.device_path == "/dev/video0"


def test_nothing_delivering_explains_the_codec_nodes(tmp_path, monkeypatch, fake_cv2):
    monkeypatch.setattr(cam, "V4L_SYSFS", _sysfs(tmp_path, {10: "bcm2835-codec-decode"}))
    fake_cv2.behaviour["/dev/video10"] = "silent"
    camera = cam.Camera(retry_s=0)
    assert not camera.open()
    assert "codec" in camera.error


def test_a_pinned_device_is_used_as_given(tmp_path, monkeypatch, fake_cv2):
    monkeypatch.setattr(cam, "V4L_SYSFS", tmp_path / "absent")
    fake_cv2.behaviour[2] = "frames"
    camera = cam.Camera(device=2, retry_s=0)
    assert camera.open() and camera.device_path == "2"

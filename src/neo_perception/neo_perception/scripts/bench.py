"""Benchmark and sanity-check a detector backend.

Serves two purposes:

* **Plan Bench B** -- on the Pi, run this to get the real ms/frame and fps at the
  chosen imgsz, which several decisions in the implementation plan depend on.
* **Format validation** -- confirms the model actually produces the COCO-17
  keypoint layout the gesture code assumes, rather than trusting it.

    python -m neo_perception.scripts.bench --source bus --runs 20
    python -m neo_perception.scripts.bench --source webcam --imgsz 320
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time

from ..detector import UltralyticsConfig, UltralyticsDetector
from ..types import KEYPOINT_NAMES

# The ultralytics package's own documented sample image, which contains people.
SAMPLE_IMAGE = "https://ultralytics.com/images/bus.jpg"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark a Neo detector backend")
    parser.add_argument("--model", default="yolov8n-pose.pt")
    parser.add_argument("--imgsz", type=int, default=320)
    parser.add_argument("--conf", type=float, default=0.35)
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument(
        "--source",
        default="bus",
        help="'bus' for the ultralytics sample, 'webcam', or an image path",
    )
    args = parser.parse_args(argv)

    frame = _load_frame(args.source)
    if frame is None:
        return 1

    detector = UltralyticsDetector(
        UltralyticsConfig(model=args.model, imgsz=args.imgsz, conf=args.conf)
    )

    print(f"model={args.model} imgsz={args.imgsz} source={args.source}")
    print("warming up...")
    detections = detector.infer(frame)

    times = []
    for _ in range(args.runs):
        start = time.perf_counter()
        detections = detector.infer(frame)
        times.append((time.perf_counter() - start) * 1000.0)

    times.sort()
    mean = statistics.fmean(times)
    print(f"\n  frames      {len(times)}")
    print(f"  mean        {mean:7.1f} ms   ({1000.0 / mean:.1f} fps)")
    print(f"  median      {statistics.median(times):7.1f} ms")
    print(f"  p90         {times[int(len(times) * 0.9)]:7.1f} ms")
    print(f"  min / max   {times[0]:7.1f} / {times[-1]:.1f} ms")

    _report_detections(detections)
    return 0


def _report_detections(detections) -> None:
    print(f"\n  detections  {len(detections)}")
    if not detections:
        print("  (no people found -- check conf, or the source image)")
        return

    with_pose = [d for d in detections if d.keypoints is not None]
    print(f"  with pose   {len(with_pose)}")
    if not with_pose:
        print("  WARNING: no keypoints. Gesture recognition needs a -pose model.")
        return

    kps = with_pose[0].keypoints
    count = len(kps.points)
    ok = count == len(KEYPOINT_NAMES)
    print(f"  keypoints   {count} ({'COCO-17, as expected' if ok else 'UNEXPECTED'})")
    if not ok:
        print("  WARNING: gesture code assumes the COCO-17 layout.")

    scale = kps.torso_scale()
    face = kps.face_anchor()
    print(f"  torso scale {scale if scale is None else f'{scale:.1f} px'}")
    print(f"  face anchor {face if face is None else f'({face[0]:.0f}, {face[1]:.0f})'}")

    for name in ("left_shoulder", "right_shoulder", "left_wrist", "right_wrist"):
        kp = kps.get(name, min_score=0.0)
        if kp is not None:
            print(f"    {name:16} ({kp.x:6.1f}, {kp.y:6.1f})  score {kp.score:.2f}")


def _load_frame(source: str):
    if source == "bus":
        return SAMPLE_IMAGE  # ultralytics fetches and caches it
    if source == "webcam":
        import cv2

        cap = cv2.VideoCapture(0)
        ok, frame = cap.read()
        cap.release()
        if not ok:
            print("could not read from the webcam", file=sys.stderr)
            return None
        return frame
    return source


if __name__ == "__main__":
    sys.exit(main())

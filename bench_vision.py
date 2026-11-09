"""Benchmark the Vision helper over the labelled set with arbitrary env overrides.

    python bench_vision.py OCR_SCALE=2 OCR_CANDIDATES=3
Evaluates each candidate string joined by '|' taking the first that is exactly 4 digits.
"""
import json
import os
import statistics
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(HERE, "build", "vision_ocr")
labels = json.load(open(os.path.join(HERE, "labels.json")))

env = dict(os.environ)
for arg in sys.argv[1:]:
    k, _, v = arg.partition("=")
    env[k] = v

paths = [f"dataset/{k}" for k in labels]
out = subprocess.run([BIN] + paths, capture_output=True, text=True, env=env).stdout
ok = wrong = skipped = 0
times = []
for line in out.strip().split("\n"):
    parts = line.split("\t")
    if len(parts) < 4:
        continue
    truth = labels[os.path.basename(parts[0])]
    try:
        times.append(float(parts[1]))
    except ValueError:
        pass
    hyps = ["".join(ch for ch in h if ch.isdigit()) for h in parts[3].split("|")]
    four = [h for h in hyps if len(h) == 4]
    if not four:
        skipped += 1
    elif four[0] == truth:
        ok += 1
    else:
        wrong += 1
print(f"{' '.join(sys.argv[1:]) or 'baseline':40s} exact={ok:2d}/{len(labels)} wrong={wrong} "
      f"abstain={skipped} median={statistics.median(times):.0f} ms")

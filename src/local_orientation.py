"""Offline document-page orientation inference and conservative decisions.

This module has no networking code.  The ONNX session is explicitly limited to
the CPU execution provider and is loaded lazily from the model bundled with the
application.  It is intentionally independent from Stage 2's paid
classification sampling: callers may inspect every PDF page in small batches
without sending page content or metadata anywhere.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path


ORIENTATIONS = (0, 90, 180, 270)
MODEL_NAME = "PP-LCNet_x1_0_doc_ori"
MODEL_REPOSITORY = "PaddlePaddle/PP-LCNet_x1_0_doc_ori_onnx"
MODEL_REVISION = "7330ab7039123e46af2dc03154b9969aa412c61d"
MODEL_SHA256 = "af9a0a4f317ff0709ce752067807f819cb15d883f8ecad89f28df1c6ee2d9c92"


def _softmax(values):
    largest = max(values)
    exps = [math.exp(float(value) - largest) for value in values]
    total = sum(exps) or 1.0
    return [value / total for value in exps]


class OnnxOrientationPredictor:
    """Small CPU-only wrapper around the official four-way Paddle model."""

    def __init__(self, model_path: Path, *, batch_size: int = 4):
        self.model_path = Path(model_path)
        self.batch_size = max(1, min(8, int(batch_size)))
        self._session = None

    def _load(self):
        if self._session is not None:
            return self._session
        if not self.model_path.is_file():
            raise FileNotFoundError(
                f"bundled orientation model is missing: {self.model_path}")
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(
            str(self.model_path), sess_options=options,
            providers=["CPUExecutionProvider"])
        return self._session

    @staticmethod
    def _prepare(image):
        """Apply the exact Resize(256 short edge), centre crop and ImageNet
        normalisation declared in the official ``inference.yml``."""
        import numpy as np
        from PIL import Image

        image = image.convert("RGB")
        width, height = image.size
        if min(width, height) <= 0:
            raise ValueError("empty orientation thumbnail")
        scale = 256.0 / min(width, height)
        resized = image.resize(
            (max(256, round(width * scale)), max(256, round(height * scale))),
            Image.Resampling.BILINEAR)
        left = max(0, (resized.width - 224) // 2)
        top = max(0, (resized.height - 224) // 2)
        cropped = resized.crop((left, top, left + 224, top + 224))
        arr = np.asarray(cropped, dtype="float32") / 255.0
        arr = (arr - np.asarray([0.485, 0.456, 0.406], dtype="float32")) \
            / np.asarray([0.229, 0.224, 0.225], dtype="float32")
        return arr.transpose(2, 0, 1)

    def predict(self, images):
        """Return one full four-way probability result for each PIL image."""
        import numpy as np

        session = self._load()
        input_name = session.get_inputs()[0].name
        output_name = session.get_outputs()[0].name
        prepared = [self._prepare(image) for image in images]
        answers = []
        for start in range(0, len(prepared), self.batch_size):
            batch = np.stack(prepared[start:start + self.batch_size]).astype(
                "float32", copy=False)
            raw = session.run([output_name], {input_name: batch})[0]
            for row in raw:
                values = [float(value) for value in row]
                # Paddle classification exports logits.  Tolerate an export
                # that already emits probabilities without applying softmax
                # twice.
                if (any(value < 0.0 or value > 1.0 for value in values)
                        or abs(sum(values) - 1.0) > 0.01):
                    values = _softmax(values)
                ranked = sorted(enumerate(values), key=lambda item: item[1],
                                reverse=True)
                top_idx, top = ranked[0]
                _runner_idx, runner = ranked[1]
                predicted = ORIENTATIONS[top_idx]
                answers.append({
                    "predicted_orientation": predicted,
                    # The model label describes the stored image orientation;
                    # correction is the inverse clockwise turn.
                    "correction": (360 - predicted) % 360,
                    "confidence": top,
                    "runner_up_confidence": runner,
                    "margin": top - runner,
                    "probabilities": {
                        str(label): values[index]
                        for index, label in enumerate(ORIENTATIONS)
                    },
                })
        return answers


def thumbnail_evidence(image) -> dict:
    """Cheap local safeguards for obviously unsuitable orientation pages.

    These are deliberately conservative vetoes, not an OCR substitute.  A
    genuine scanned text page remains eligible; a blank/sparse page or a very
    strongly photographic colour image does not.
    """
    import numpy as np

    rgb = image.convert("RGB")
    rgb.thumbnail((256, 256))
    arr = np.asarray(rgb, dtype="uint8")
    if arr.size == 0:
        return {"blank": True, "sparse": True, "photograph_only": False,
                "ink_fraction": 0.0}
    grey = (arr[:, :, 0].astype("float32") * 0.299
            + arr[:, :, 1].astype("float32") * 0.587
            + arr[:, :, 2].astype("float32") * 0.114)
    ink_fraction = float((grey < 245).mean())
    colour_spread = (arr.max(axis=2).astype("int16")
                     - arr.min(axis=2).astype("int16"))
    colour_fraction = float((colour_spread > 35).mean())
    white_fraction = float((grey > 245).mean())
    blank = ink_fraction < 0.003
    sparse = ink_fraction < 0.012
    photograph_only = colour_fraction > 0.45 and white_fraction < 0.25
    return {
        "blank": blank,
        "sparse": sparse,
        "photograph_only": photograph_only,
        "ink_fraction": ink_fraction,
        "colour_fraction": colour_fraction,
        "white_fraction": white_fraction,
    }


def orientation_decision(prediction: dict, evidence: dict, *, mode: str,
                         confidence_threshold: float,
                         margin_threshold: float,
                         text_correction=None) -> dict:
    """Combine a prediction with local safety gates.

    ``rotate`` is non-zero only in automatic mode and only after all guards
    pass.  Audit mode records the same decision evidence without mutating the
    document.
    """
    predicted = int(prediction.get("predicted_orientation", 0) or 0)
    correction = int(prediction.get("correction", 0) or 0)
    confidence = float(prediction.get("confidence", 0.0) or 0.0)
    runner = float(prediction.get("runner_up_confidence", 0.0) or 0.0)
    margin = float(prediction.get("margin", confidence - runner) or 0.0)
    reason = "upright"
    eligible = False
    uncertain = False

    if evidence.get("blank"):
        reason, uncertain = "blank page", True
    elif evidence.get("sparse"):
        reason, uncertain = "sparse page", True
    elif evidence.get("photograph_only"):
        reason, uncertain = "photograph-only page", True
    elif (text_correction is not None
          and int(text_correction) != correction):
        reason, uncertain = "local model conflicts with PDF text direction", True
    elif confidence < float(confidence_threshold):
        reason, uncertain = "confidence below automatic threshold", True
    elif margin < float(margin_threshold):
        reason, uncertain = "confidence margin below automatic threshold", True
    elif correction:
        reason, eligible = "high-confidence rotated page", True

    return {
        "predicted_orientation": predicted,
        "correction": correction,
        "confidence": confidence,
        "runner_up_confidence": runner,
        "margin": margin,
        "reason": reason,
        "uncertain": uncertain,
        "eligible": eligible,
        "rotate": correction if mode == "automatic" and eligible else 0,
    }


def atomic_write_json(path: Path, data: dict):
    """Durably replace a JSON state file from a same-directory temporary."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                    dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

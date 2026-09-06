# Bundled document-orientation model

- Model: `PP-LCNet_x1_0_doc_ori`
- Format: official ONNX export (`inference.onnx`)
- Publisher/source: PaddlePaddle, `PaddlePaddle/PP-LCNet_x1_0_doc_ori_onnx`
- Pinned repository revision: `7330ab7039123e46af2dc03154b9969aa412c61d`
- Model SHA-256: `af9a0a4f317ff0709ce752067807f819cb15d883f8ecad89f28df1c6ee2d9c92`
- Model size: 6,788,069 bytes
- Licence: Apache License 2.0 (the accompanying `LICENSE` is copied from the
  official PaddleOCR repository)
- Verified: 2026-09-02

The model has four output labels: 0, 90, 180 and 270 degrees. Stage 2 runs it
only with ONNX Runtime's CPU execution provider. The model is shipped inside
the frozen application and is never downloaded at runtime.

Official references:

- https://huggingface.co/PaddlePaddle/PP-LCNet_x1_0_doc_ori_onnx
- https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/module_usage/doc_img_orientation_classification.en.md
- https://github.com/PaddlePaddle/PaddleOCR/blob/main/LICENSE

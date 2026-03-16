#!/bin/bash

MODEL_DIR="sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"

exec python -m kws \
--tokens ./$MODEL_DIR/tokens.txt \
--encoder ./$MODEL_DIR/encoder-epoch-99-avg-1-chunk-16-left-64.int8.onnx \
--decoder ./$MODEL_DIR/decoder-epoch-99-avg-1-chunk-16-left-64.int8.onnx \
--joiner ./$MODEL_DIR/joiner-epoch-99-avg-1-chunk-16-left-64.int8.onnx \
--keywords-file ./keywords.txt \
--keywords-threshold 0.03 \
--keywords-score 2.5
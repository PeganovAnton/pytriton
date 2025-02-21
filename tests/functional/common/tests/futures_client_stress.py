# Copyright (c) 2023, NVIDIA CORPORATION. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Runs inference session over  NLP model
"""

import logging
import pathlib
import tempfile
import textwrap
from concurrent.futures import FIRST_COMPLETED, wait
from typing import Callable

import numpy as np

from pytriton.client import FuturesModelClient
from pytriton.decorators import batch
from pytriton.model_config import DynamicBatcher, ModelConfig, Tensor
from pytriton.triton import Triton, TritonConfig
from tests.functional.common.models import Framework, TestModelSpec
from tests.utils import find_free_port

logger = logging.getLogger(__package__)

VOCABULARY_SIZE = 30522
VALID_TOKEN_ID = 5
MIN_SEQUENCE_LENGTH = 20
MAX_SEQUENCE_LENGTH = 128
RANDOM_SEED = 42


def futures_stress_test(test_time_s: int, init_timeout_s: int, batch_size: int, seed: int, verbose: bool):

    model_name = "distilbert-base-uncased"

    model_spec = _model_spec()

    import random

    random.seed(seed)

    def requests_generator():
        while True:
            inputs_len = random.randint(MIN_SEQUENCE_LENGTH, MAX_SEQUENCE_LENGTH)
            input_ids = (
                np.zeros(
                    (
                        1,
                        inputs_len,
                    ),
                    dtype=np.int64,
                )
                + 5
            )
            attention_mask = np.ones(
                (
                    1,
                    inputs_len,
                ),
                dtype=np.int64,
            )
            yield {"input_ids": input_ids, "attention_mask": attention_mask}

    requests = requests_generator()

    logger.info("starting server")

    infer_fn = model_spec.create_infer_fn(model_name=model_name)
    with tempfile.TemporaryDirectory() as temp_dir:
        triton_log_path = pathlib.Path(temp_dir) / "triton.log"
        try:
            triton_config = TritonConfig(
                grpc_port=find_free_port(),
                http_port=find_free_port(),
                metrics_port=find_free_port(),
                log_verbose=int(verbose),
                log_file=triton_log_path,
            )
            with Triton(config=triton_config) as triton:
                triton.bind(
                    model_name=model_spec.name,
                    infer_func=infer_fn,
                    inputs=model_spec.inputs,
                    outputs=model_spec.outputs,
                    config=model_spec.model_config,
                )
                triton.run()

                # Send requests
                url = f"http://localhost:{triton_config.http_port}"
                with FuturesModelClient(url, model_spec.name, max_workers=batch_size) as client:
                    # Wait for model
                    client.wait_for_model(init_timeout_s).result()

                    import time

                    should_stop_at_s = time.time() + test_time_s

                    number_of_processed_requests = 0

                    not_done = {*()}
                    for request in requests:
                        result_future = client.infer_batch(**request)
                        not_done.add(result_future)
                        if len(not_done) > batch_size:
                            done, not_done = wait(not_done, return_when=FIRST_COMPLETED)
                            if len(done) > 0:
                                future = done.pop()
                                result = future.result()
                                number_of_processed_requests += len(done)
                                if number_of_processed_requests > 0 and number_of_processed_requests % 10 == 0:
                                    time_left_s = max(should_stop_at_s - time.time(), 0.0)
                                    logger.debug(
                                        f"Processed {number_of_processed_requests} batches time left: {time_left_s:0.1f}s \n."
                                        f"Result: {len(result)}."
                                    )
                        time_left_s = max(should_stop_at_s - time.time(), 0.0)
                        if time_left_s % 10 == 0:
                            logger.info(f"Time left: {time_left_s:0.1f}s")
                        if time_left_s <= 0:
                            break
                logger.info(f"Test finished. Processed {number_of_processed_requests} requests")

        finally:
            if triton_log_path.exists():
                logger.debug("-" * 64)
                server_logs = triton_log_path.read_text(errors="replace")
                server_logs = "--- triton logs:\n\n" + textwrap.indent(server_logs, prefix=" " * 8)
                logger.debug(server_logs)
    logger.info("Test finished")


def _create_hf_tensorflow_distilbert_base_uncased_fn(model_name: str) -> Callable:
    @batch
    def _infer_fn(input_ids, attention_mask):
        assert input_ids.shape == attention_mask.shape
        import random

        outputs_len = random.randint(20, 128)
        result = np.zeros([input_ids.shape[0], outputs_len, VOCABULARY_SIZE], dtype=np.float32)
        logger.debug(f"input_ids: {input_ids.shape}")
        logger.debug(f"attention_mask: {attention_mask.shape}")
        return {"logits": result}

    return _infer_fn


def _model_spec() -> TestModelSpec:
    model_spec = TestModelSpec(
        name="DistilBert",
        framework=Framework.TENSORFLOW,
        create_infer_fn=_create_hf_tensorflow_distilbert_base_uncased_fn,
        inputs=(
            Tensor(name="input_ids", dtype=np.int64, shape=(-1,)),
            Tensor(name="attention_mask", dtype=np.int64, shape=(-1,)),
        ),
        outputs=(
            Tensor(
                name="logits",
                dtype=np.float32,
                shape=(-1, -1),
            ),
        ),
        model_config=ModelConfig(
            max_batch_size=16,
            batcher=DynamicBatcher(
                max_queue_delay_microseconds=5000,
            ),
        ),
    )
    return model_spec

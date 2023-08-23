#!/usr/bin/env python3
# Copyright (c) 2022, NVIDIA CORPORATION.  All rights reserved.
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
"""Text generation server with NeMo Megatron GPT model."""
import argparse
import logging

import torch  # pytype: disable=import-error
from nemo.collections.nlp.modules.common.text_generation_utils import generate  # pytype: disable=import-error
from nemo.collections.nlp.parts.nlp_overrides import NLPDDPStrategy
from pytorch_lightning.trainer.trainer import Trainer  # pytype: disable=import-error

from pytriton.model_config import ModelConfig
from pytriton.triton import Triton, TritonConfig

from gpt import NemoGptCallable  # pytype: disable=import-error # isort:skip
from helpers import (  # pytype: disable=import-error # isort:skip
    download_hf_model,
    load_model,
    setup_distributed_environment,
)

if not torch.cuda.is_available():
    raise OSError("GPU is needed for the inference")

ENDPOINT_BIND_ADDRESS = "0.0.0.0"
HTTP_PORT = 8000
DEFAULT_LOG_FORMAT = "%(asctime)s - %(levelname)8s - %(process)8d - %(threadName)s - %(name)s: %(message)s"


def main():
    """Main function."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gpus",
        default="-1",
        help=(
            "Number of GPUs to load model on or exact identifiers of GPUs to use separated by comma. "
            "If set to -1 all available GPU will be used."
        ),
    )
    parser.add_argument(
        "--nodes",
        default=1,
        type=int,
        help="Number of nodes to load model on",
    )
    parser.add_argument(
        "--model-repo-id",
        default="nvidia/nemo-megatron-gpt-1.3B",
        help="Model repository id on HuggingFace Hub",
    )
    parser.add_argument(
        "--model-filename",
        help="Path to the model nemo file in HF hub. If not provided first on the list .nemo file will be used.",
    )
    parser.add_argument("--prompt-model-path", help="Path to the model prompt nemo file")
    parser.add_argument(
        "--timeout",
        default=30,
        type=int,
        required=False,
        help="Process group communication timeout",
    )
    parser.add_argument(
        "--verbose",
        default=False,
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level, format=DEFAULT_LOG_FORMAT, force=True)
    logger = logging.getLogger("nemo.server")

    logger.info("Initialize trainer:")
    logger.info(f" devices: {args.gpus}")
    logger.info(f" nodes: {args.nodes}")
    strategy = NLPDDPStrategy(
        no_ddp_communication_hook=True,  # we don't use DDP for async grad allreduce
        gradient_as_bucket_view=False,
        find_unused_parameters=False,
    )

    trainer = Trainer(
        plugins=[],
        strategy=strategy,
        devices=nemo_params["devices"],
        accelerator="gpu",
        num_nodes=nemo_params["num_nodes"],
        precision=16,
        logger=False,
        enable_checkpointing=False,
        replace_sampler_ddp=False,
    )

    model_path = download_hf_model(args.model_repo_id, args.model_filename)
    model = load_model(model_path, trainer, prompt_learning_model_path=args.prompt_model_path)

    app_state = setup_distributed_environment(trainer)
    if app_state.global_rank == 0:

        infer_callable = NemoGptCallable(model_name="GPT", model=model)

        triton_config = TritonConfig(http_address=ENDPOINT_BIND_ADDRESS, http_port=HTTP_PORT)
        with Triton(config=triton_config) as triton:
            triton.bind(
                model_name=infer_callable.model_name,
                infer_func=infer_callable.infer,
                inputs=infer_callable.inputs,
                outputs=infer_callable.outputs,
                config=ModelConfig(max_batch_size=128),
            )

            triton.serve()
    else:
        logger.info(f"Running worker with rank {torch.distributed.get_rank()}")
        while True:
            choice = torch.cuda.LongTensor(1)
            torch.distributed.broadcast(choice, 0)
            logger.info(f"{choice}")
            if choice[0].item() == 0:
                generate(model.cuda())


if __name__ == "__main__":
    main()

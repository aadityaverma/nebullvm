import abc
import copy
import os
import subprocess
from pathlib import Path
from typing import List, Any, Tuple

import numpy as np

from nebullvm.config import QUANTIZATION_DATA_NUM, TORCH_TENSORRT_PRECISIONS
from nebullvm.operations.optimizations.compilers.base import Compiler

from nebullvm.operations.optimizations.compilers.quantizations.tensor_rt import (  # noqa: E501
    quantize_tensorrt,
)
from nebullvm.operations.optimizations.compilers.quantizations.utils import (
    check_quantization,
)
from nebullvm.optional_modules.tensor_rt import tensorrt as trt
from nebullvm.optional_modules.torch import torch, Module
from nebullvm.optional_modules.torch_tensorrt import (
    torch_tensorrt,
    DataLoaderCalibrator,
)
from nebullvm.tools.base import (
    QuantizationType,
    ModelParams,
)
from nebullvm.tools.data import DataManager, PytorchDataset
from nebullvm.tools.onnx import get_input_names
from nebullvm.tools.transformations import (
    MultiStageTransformation,
    HalfPrecisionTransformation,
)


class TensorRTCompiler(Compiler, abc.ABC):
    supported_ops = {
        "cpu": [],
        "gpu": [
            None,
            QuantizationType.STATIC,
            QuantizationType.HALF,
        ],
    }

    def __init__(self):
        super().__init__()
        self.model_orig = None

    @abc.abstractmethod
    def execute(self, *args, **kwargs):
        pass


class PyTorchTensorRTCompiler(TensorRTCompiler):
    def execute(
        self,
        model: Module,
        model_params: ModelParams,
        input_tfms: MultiStageTransformation = None,
        metric_drop_ths: float = None,
        quantization_type: QuantizationType = None,
        input_data: DataManager = None,
        **kwargs,
    ):
        """Compile the input model using TensorRT Compiler from the
            PyTorch interface.

        Args:
            model (torch.nn.Module): The pytorch model.
            model_params (ModelParams): The model parameters.
            input_tfms (MultiStageTransformation, optional): Transformations
                to be performed to the model's input tensors in order to
                get the prediction. Default: None.
            metric_drop_ths (float, optional): Threshold for the accepted drop
                in terms of precision. Any optimized model with a higher drop
                will be ignored. Default: None.
            quantization_type (QuantizationType, optional): The desired
                quantization algorithm to be used. Default: None.
            input_data (DataManager): User defined data. Default: None
        """

        if quantization_type not in self.supported_ops[self.device.value]:
            self.compiled_model = None
            return

        if quantization_type is QuantizationType.STATIC and input_data is None:
            raise ValueError("Input data is required for static quantization.")

        self.logger.info(
            f"Optimizing with {self.__class__.__name__} and "
            f"q_type: {quantization_type}."
        )

        check_quantization(quantization_type, metric_drop_ths)

        if quantization_type is QuantizationType.HALF:
            dtype = torch.half
            input_tfms.append(HalfPrecisionTransformation())
        elif quantization_type is QuantizationType.STATIC:
            dtype = torch.int8

            dataset = PytorchDataset(input_data.get_split("train"))
            dataloader = torch.utils.data.DataLoader(
                dataset,
                batch_size=dataset.batch_size,
                shuffle=False,
                num_workers=0,
            )

            calibrator = torch_tensorrt.ptq.DataLoaderCalibrator(
                dataloader,
                use_cache=False,
                algo_type=torch_tensorrt.ptq.CalibrationAlgo.ENTROPY_CALIBRATION_2,  # noqa E501
                device=torch.device("cuda:0"),
            )
        else:
            dtype = torch.float32

        # Convert int64 to int32 for transformers inputs
        input_tensors = [
            tensor.cuda()
            if tensor.dtype != torch.int64
            else tensor.to(torch.int32).cuda()
            for tensor in input_data.get_list(1)[0]
        ]

        self.compiled_model = self._compile_model(
            model=model,
            input_tensors=input_tensors,
            dtype=dtype,
            calibrator=calibrator
            if quantization_type is QuantizationType.STATIC
            else None,  # noqa E501
            quantization_type=quantization_type,
        )

    def _compile_model(
        self,
        model: Module,
        input_tensors: List[torch.Tensor],
        dtype: torch.dtype,
        calibrator: DataLoaderCalibrator,
        quantization_type: QuantizationType,
    ):

        model.cuda().eval()

        try:
            torch.jit.script(model)
        except Exception:
            model = torch.jit.trace(model, input_tensors)

        with torch_tensorrt.logging.errors():
            trt_model = torch_tensorrt.compile(
                model
                if dtype is not torch.half
                else copy.deepcopy(model).half(),
                inputs=[
                    torch_tensorrt.Input(
                        tensor.shape,
                        dtype=torch.half
                        if (
                            dtype == torch.half
                            and tensor.dtype not in [torch.int8, torch.int32]
                        )
                        else tensor.dtype,
                    )
                    for tensor in input_tensors
                ],
                enabled_precisions=TORCH_TENSORRT_PRECISIONS[str(dtype)],
                calibrator=calibrator
                if quantization_type is QuantizationType.STATIC
                else None,
                workspace_size=1 << 30,
                device={
                    "device_type": torch_tensorrt.DeviceType.GPU,
                    "gpu_id": 0,
                    "dla_core": 0,
                    "allow_gpu_fallback": False,
                    "disable_tf32": False,
                },
                truncate_long_and_double=True,
            )

        # Delete calibration cache
        if os.path.exists("calibration.cache"):
            os.remove("calibration.cache")

        return trt_model

    @staticmethod
    def _quantize_model(**kwargs) -> Any:
        raise NotImplementedError


class ONNXTensorRTCompiler(TensorRTCompiler):
    def __init__(self):
        super().__init__()
        self.model_orig = None

    def execute(
        self,
        model: str,
        model_params: ModelParams,
        input_tfms: MultiStageTransformation = None,
        metric_drop_ths: float = None,
        quantization_type: QuantizationType = None,
        input_data: DataManager = None,
        **kwargs,
    ):
        """Compile the input model using TensorRT Compiler from the
            ONNX interface.

        Args:
            model (str): The path to the onnx model.
            model_params (ModelParams): The model parameters.
            input_tfms (MultiStageTransformation, optional): Transformations
                to be performed to the model's input tensors in order to
                get the prediction. Default: None.
            metric_drop_ths (float, optional): Threshold for the accepted drop
                in terms of precision. Any optimized model with a higher drop
                will be ignored. Default: None.
            quantization_type (QuantizationType, optional): The desired
                quantization algorithm to be used. Default: None.
            input_data (DataManager): User defined data. Default: None
        """

        if quantization_type not in self.supported_ops[self.device.value]:
            self.compiled_model = None
            return

        if quantization_type is QuantizationType.STATIC and input_data is None:
            raise ValueError("Input data is required for static quantization.")

        self.logger.info(
            f"Optimizing with {self.__class__.__name__} and "
            f"q_type: {quantization_type}."
        )

        check_quantization(quantization_type, metric_drop_ths)
        train_input_data = input_data.get_split("train").get_numpy_list(
            QUANTIZATION_DATA_NUM
        )

        try:
            import onnxsim  # noqa: F401

            # Simplify model, otherwise tensor RT won't work on gpt2 and some
            # other models.
            simplified_model = str(model) + "_simplified"
            if not Path(simplified_model).is_file():
                cmd = [
                    "onnxsim",
                    str(model),
                    simplified_model,
                ]
                subprocess.run(cmd, stdout=subprocess.DEVNULL)

            # First try with simplified model
            onnx_model_path = simplified_model
        except Exception:
            # Try again with original model
            onnx_model_path = str(model)

        # -- Build phase --
        nvidia_logger = trt.Logger(trt.Logger.ERROR)
        builder = trt.Builder(nvidia_logger)
        # create network definition
        network = builder.create_network(
            1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH)
        )
        # build the engine
        # TODO: setup config value for the class in a config file
        config = builder.create_builder_config()
        try:
            config.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, 1 << 30)
        except AttributeError:
            # The method set_memory_pool_limit is not available
            # until TensorRT Release 8.4.1
            self.logger.warning(
                "Cannot call method set_memory_pool_limit for TensorRT."
                "Please update TensorRT version."
            )

        if quantization_type is not None:
            config = self._quantize_model(
                quantization_type,
                model_params,
                config,
                input_tfms,
                train_input_data
                if quantization_type is QuantizationType.STATIC
                else None,
            )

        self.compiled_model = self._compile_model(
            onnx_model_path=str(onnx_model_path),
            model_params=model_params,
            config=config,
            network=network,
            builder=builder,
            nvidia_logger=nvidia_logger,
        )
        self.model_orig = onnx_model_path

    def _compile_model(
        self,
        onnx_model_path: str,
        model_params: ModelParams,
        config,
        network,
        builder,
        nvidia_logger,
    ):
        parser = trt.OnnxParser(network, nvidia_logger)
        success = parser.parse_from_file(onnx_model_path)

        if not success:
            for idx in range(parser.num_errors):
                self.logger.debug(parser.get_error(idx))
            raise ValueError(
                f"Errors occurred while processing the "
                f"ONNX file at {onnx_model_path}"
            )

        if model_params.dynamic_info is not None:
            profile = builder.create_optimization_profile()
            for input_name, input_dynamic_info, input_info in zip(
                get_input_names(onnx_model_path),
                model_params.dynamic_info.inputs,
                model_params.input_infos,
            ):
                profile.set_shape(
                    input_name,
                    (
                        min(model_params.batch_size, 1)
                        if 0 in input_dynamic_info
                        else model_params.batch_size,
                        *(
                            shape
                            if i + 1 not in input_dynamic_info
                            else (input_info.min_sizes or {}).get(i + 1, 1)
                            for i, shape in enumerate(input_info.size)
                        ),
                    ),
                    (model_params.batch_size, *input_info.size),
                    (model_params.batch_size, *input_info.size),
                )
            config.add_optimization_profile(profile)
        return builder.build_serialized_network(network, config)

    @staticmethod
    def _quantize_model(
        quantization_type: QuantizationType,
        model_params: ModelParams,
        config,
        input_tfms: MultiStageTransformation,
        input_data: List[Tuple[np.ndarray, ...]] = None,
    ):
        return quantize_tensorrt(
            quantization_type,
            model_params,
            config,
            input_tfms,
            input_data,
        )

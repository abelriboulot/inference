import numpy as np
import pytest

from inference.core.entities.responses.inference import (
    ClassificationInferenceResponse,
    MultiLabelClassificationInferenceResponse,
)
from inference.core.env import MAX_BATCH_SIZE
from inference.models import VitClassification
from tests.common import assert_classification_predictions_match


@pytest.mark.slow
def test_vit_multi_class_single_image_inference(
    vit_multi_class_model: str,
    example_image: np.ndarray,
    vit_multi_class_reference_prediction: ClassificationInferenceResponse,
) -> None:
    # given
    model = VitClassification(model_id=vit_multi_class_model, api_key="DUMMY")

    # when
    result = model.infer(example_image)

    # then
    assert len(result) == 1, "Batch size=1 hence 1 result expected"
    assert_classification_predictions_match(prediction_1=result[0], prediction_2=vit_multi_class_reference_prediction, confidence_tolerance=1e-4)


@pytest.mark.slow
def test_vit_multi_class_batch_inference_when_batch_size_smaller_than_max_batch_size(
    vit_multi_class_model: str,
    example_image: np.ndarray,
    vit_multi_class_reference_prediction: ClassificationInferenceResponse,
) -> None:
    # given
    batch_size = min(4, MAX_BATCH_SIZE)
    model = VitClassification(model_id=vit_multi_class_model, api_key="DUMMY")

    # when
    result = model.infer([example_image] * batch_size)

    # then
    assert len(result) == batch_size, "Number of results must match batch size"
    reference_prediction = result[0]
    assert all(
        p == reference_prediction for p in result
    ), "All predictions must be the same as input was re-used"
    assert_classification_predictions_match(prediction_1=result[0], prediction_2=vit_multi_class_reference_prediction, confidence_tolerance=1e-4)


@pytest.mark.slow
@pytest.mark.skipif(
    MAX_BATCH_SIZE > 8,
    reason="This test requires reasonably small MAX_BATCH_SIZE set via environment variable",
)
def test_vit_multi_class_batch_inference_when_batch_size_larger_then_max_batch_size(
    vit_multi_class_model: str,
    example_image: np.ndarray,
    vit_multi_class_reference_prediction: ClassificationInferenceResponse,
) -> None:
    # given
    batch_size = MAX_BATCH_SIZE + 2
    model = VitClassification(model_id=vit_multi_class_model, api_key="DUMMY")

    # when
    result = model.infer([example_image] * batch_size)

    # then
    assert len(result) == batch_size, "Number of results must match batch size"
    reference_prediction = result[0]
    assert all(
        p == reference_prediction for p in result
    ), "All predictions must be the same as input was re-used"
    assert_classification_predictions_match(prediction_1=result[0], prediction_2=vit_multi_class_reference_prediction, confidence_tolerance=1e-4)


@pytest.mark.slow
def test_vit_multi_label_single_image_inference(
    vit_multi_label_model: str,
    example_image: np.ndarray,
    vit_multi_label_reference_prediction: MultiLabelClassificationInferenceResponse,
) -> None:
    # given
    model = VitClassification(model_id=vit_multi_label_model, api_key="DUMMY")

    # when
    result = model.infer(example_image)

    # then
    assert len(result) == 1, "Batch size=1 hence 1 result expected"
    assert_classification_predictions_match(prediction_1=result[0], prediction_2=vit_multi_label_reference_prediction)


@pytest.mark.slow
def test_vit_multi_label_batch_inference_when_batch_size_smaller_than_max_batch_size(
    vit_multi_label_model: str,
    example_image: np.ndarray,
    vit_multi_label_reference_prediction: MultiLabelClassificationInferenceResponse,
) -> None:
    # given
    batch_size = min(4, MAX_BATCH_SIZE)
    model = VitClassification(model_id=vit_multi_label_model, api_key="DUMMY")

    # when
    result = model.infer([example_image] * batch_size)

    # then
    assert len(result) == batch_size, "Number of results must match batch size"
    reference_prediction = result[0]
    assert all(
        p == reference_prediction for p in result
    ), "All predictions must be the same as input was re-used"
    assert_classification_predictions_match(prediction_1=result[0], prediction_2=vit_multi_label_reference_prediction)


@pytest.mark.slow
@pytest.mark.skipif(
    MAX_BATCH_SIZE > 8,
    reason="This test requires reasonably small MAX_BATCH_SIZE set via environment variable",
)
def test_vit_multi_label_batch_inference_when_batch_size_larger_then_max_batch_size(
    vit_multi_label_model: str,
    example_image: np.ndarray,
    vit_multi_label_reference_prediction: MultiLabelClassificationInferenceResponse,
) -> None:
    # given
    batch_size = MAX_BATCH_SIZE + 2
    model = VitClassification(model_id=vit_multi_label_model, api_key="DUMMY")

    # when
    result = model.infer([example_image] * batch_size)

    # then
    assert len(result) == batch_size, "Number of results must match batch size"
    reference_prediction = result[0]
    assert all(
        p == reference_prediction for p in result
    ), "All predictions must be the same as input was re-used"
    assert_classification_predictions_match(prediction_1=result[0], prediction_2=vit_multi_label_reference_prediction)

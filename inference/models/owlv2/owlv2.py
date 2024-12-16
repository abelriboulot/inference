import hashlib
import os
import pickle
from collections import defaultdict
from typing import Any, Dict, List, Literal, NewType, Tuple, Union

import numpy as np
import torch
import torch.nn.functional as F
import torchvision
from transformers import Owlv2ForObjectDetection, Owlv2Processor
from transformers.models.owlv2.modeling_owlv2 import box_iou

from inference.core.entities.responses.inference import (
    InferenceResponseImage,
    ObjectDetectionInferenceResponse,
    ObjectDetectionPrediction,
)
from inference.core.env import DEVICE, MAX_DETECTIONS
from inference.core.models.roboflow import (
    DEFAULT_COLOR_PALETTE,
    RoboflowCoreModel,
    draw_detection_predictions,
)
from inference.core.utils.image_utils import (
    ImageType,
    extract_image_payload_and_type,
    load_image_rgb,
)

# TYPES
Hash = NewType("Hash", str)
PosNegKey = Literal["positive", "negative"]
PosNegDictType = Dict[PosNegKey, torch.Tensor]
QuerySpecType = Dict[Hash, List[List[int]]]
if DEVICE is None:
    DEVICE = "cuda:0" if torch.cuda.is_available() else "cpu"


def to_corners(box):
    cx, cy, w, h = box.unbind(-1)
    x1 = cx - w / 2
    y1 = cy - h / 2
    x2 = cx + w / 2
    y2 = cy + h / 2
    return torch.stack([x1, y1, x2, y2], dim=-1)


def from_corners(box):
    x1, y1, x2, y2 = box.unbind(-1)
    cx = (x1 + x2) / 2
    cy = (y1 + y2) / 2
    w = x2 - x1
    h = y2 - y1
    return torch.stack([cx, cy, w, h], dim=-1)



from collections import OrderedDict


class LimitedSizeDict(OrderedDict):
    def __init__(self, *args, **kwds):
        self.size_limit = kwds.pop("size_limit", None)
        OrderedDict.__init__(self, *args, **kwds)
        self._check_size_limit()

    def __setitem__(self, key, value):
        OrderedDict.__setitem__(self, key, value)
        self._check_size_limit()

    def _check_size_limit(self):
        if self.size_limit is not None:
            while len(self) > self.size_limit:
                self.popitem(last=False)


def preprocess_image(
    np_image: np.ndarray,
    image_size: Tuple[int, int],
    image_mean: torch.Tensor,
    image_std: torch.Tensor,
) -> torch.Tensor:
    """Preprocess an image for OWLv2 by resizing, normalizing, and padding it.
    This is much faster than using the Owlv2Processor directly, as we ensure we use GPU if available.

    Args:
        np_image (np.ndarray): The image to preprocess, with shape (H, W, 3)
        image_size (tuple[int, int]): The target size of the image
        image_mean (torch.Tensor): The mean of the image, on DEVICE, with shape (1, 3, 1, 1)
        image_std (torch.Tensor): The standard deviation of the image, on DEVICE, with shape (1, 3, 1, 1)

    Returns:
        torch.Tensor: The preprocessed image, on DEVICE, with shape (1, 3, H, W)
    """
    current_size = np_image.shape[:2]

    r = min(image_size[0] / current_size[0], image_size[1] / current_size[1])
    target_size = (int(r * current_size[0]), int(r * current_size[1]))

    torch_image = (
        torch.tensor(np_image)
        .permute(2, 0, 1)
        .unsqueeze(0)
        .to(DEVICE)
        .to(dtype=torch.float32)
        / 255.0
    )
    torch_image = F.interpolate(
        torch_image, size=target_size, mode="bilinear", align_corners=False
    )

    padded_image_tensor = torch.ones((1, 3, *image_size), device=DEVICE) * 0.5
    padded_image_tensor[:, :, : torch_image.shape[2], : torch_image.shape[3]] = (
        torch_image
    )

    padded_image_tensor = (padded_image_tensor - image_mean) / image_std

    return padded_image_tensor


def filter_tensors_by_objectness(
    objectness: torch.Tensor,
    boxes: torch.Tensor,
    image_class_embeds: torch.Tensor,
    logit_shift: torch.Tensor,
    logit_scale: torch.Tensor,
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
    objectness = objectness.squeeze(0)
    # max_detections = min(MAX_DETECTIONS, int(0.05 * objectness.numel()))
    # max_detections = objectness.numel()
    max_detections = MAX_DETECTIONS
    objectness, objectness_indices = torch.topk(objectness, max_detections, dim=0)
    boxes = boxes.squeeze(0)
    image_class_embeds = image_class_embeds.squeeze(0)
    logit_shift = logit_shift.squeeze(0).squeeze(1)
    logit_scale = logit_scale.squeeze(0).squeeze(1)
    boxes = boxes[objectness_indices]
    image_class_embeds = image_class_embeds[objectness_indices]
    logit_shift = logit_shift[objectness_indices]
    logit_scale = logit_scale[objectness_indices]
    return objectness, boxes, image_class_embeds, logit_shift, logit_scale


def get_class_preds_from_embeds(
    pos_neg_embedding_dict: PosNegDictType,
    image_class_embeds: torch.Tensor,
    confidence: float,
    image_boxes: torch.Tensor,
    class_map: Dict[Tuple[str, str], int],
    class_name: str,
    iou_threshold: float,
    objectness: torch.Tensor,
    logit_shift: torch.Tensor,
    logit_scale: torch.Tensor,
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    predicted_boxes_per_class = []
    predicted_class_indices_per_class = []
    predicted_scores_per_class = []
    positive_arr_per_class = []
    for positive, embedding in pos_neg_embedding_dict.items():
        if embedding is None:
            continue
        pred_logits = torch.einsum("sd,nd->ns", image_class_embeds, embedding)
        prediction_scores = pred_logits.max(dim=0)[0]
        # prediction_scores = (prediction_scores + logit_shift) * logit_scale
        # prediction_scores = prediction_scores.sigmoid()
        # prediction_scores = (prediction_scores + 1) / 2
        # prediction_scores = 0.5*prediction_scores + 0.2
        # prediction_scores = prediction_scores * objectness
        score_mask = prediction_scores > confidence
        predicted_boxes_per_class.append(image_boxes[score_mask])
        scores = prediction_scores[score_mask]
        predicted_scores_per_class.append(scores)
        class_ind = class_map[(class_name, positive)]
        predicted_class_indices_per_class.append(class_ind * torch.ones_like(scores))
        positive_arr_per_class.append(
            int(positive == "positive") * torch.ones_like(scores)
        )

    if not predicted_boxes_per_class:
        return (
            torch.empty((0, 4)),
            torch.empty((0,)),
            torch.empty((0,)),
        )

    # concat tensors
    pred_boxes = torch.cat(predicted_boxes_per_class, dim=0).float()
    pred_classes = torch.cat(predicted_class_indices_per_class, dim=0).float()
    pred_scores = torch.cat(predicted_scores_per_class, dim=0).float()
    positive = torch.cat(positive_arr_per_class, dim=0).float()
    # nms
    survival_indices = torchvision.ops.nms(
        to_corners(pred_boxes), pred_scores, iou_threshold
    )
    # filter to post-nms
    pred_boxes = pred_boxes[survival_indices, :]
    pred_classes = pred_classes[survival_indices]
    pred_scores = pred_scores[survival_indices]
    positive = positive[survival_indices]
    is_positive = positive == 1
    # return only positive elements of tensor
    return pred_boxes[is_positive], pred_classes[is_positive], pred_scores[is_positive]


def make_class_map(
    query_embeddings: Dict[str, PosNegDictType]
) -> Tuple[Dict[Tuple[str, str], int], List[str]]:
    class_names = sorted(list(query_embeddings.keys()))
    class_map_positive = {
        (class_name, "positive"): i for i, class_name in enumerate(class_names)
    }
    class_map_negative = {
        (class_name, "negative"): i + len(class_names)
        for i, class_name in enumerate(class_names)
    }
    class_map = {**class_map_positive, **class_map_negative}
    return class_map, class_names


def hash_function(value: Any) -> Hash:
    # wrapper so we can change the hashing function in the future
    return hashlib.sha1(value).hexdigest()


class LazyImageRetrievalWrapper:
    def __init__(self, image: Any):
        self.image = image

        self._image_as_numpy = None
        self._image_hash = None

    @property
    def image_as_numpy(self) -> np.ndarray:
        if self._image_as_numpy is None:
            self._image_as_numpy = load_image_rgb(self.image)
        return self._image_as_numpy

    @property
    def image_hash(self) -> Hash:
        if self._image_hash is None:
            image_payload, image_type = extract_image_payload_and_type(self.image)
            if image_type is ImageType.URL:
                # we can use the url as the hash
                self._image_hash = image_payload
            elif image_type is ImageType.BASE64:
                # this is presumably the compressed image bytes
                # hashing this directly is faster than loading the raw image through numpy
                # we have to make sure we're passing a buffer, so we encode to bytes if necessary
                # see load_image_base64 in image_utils.py for more details about the base64 encoding
                if type(image_payload) is str:
                    image_payload = image_payload.encode("utf-8")
                self._image_hash = hash_function(image_payload)
            else:
                # not clear that there is something safe or faster to do than just loading the numpy array
                # and hashing that
                self._image_hash = hash_function(self.image_as_numpy.tobytes())
        return self._image_hash


def hash_wrapped_training_data(wrapped_training_data: List[Dict[str, Any]]) -> Hash:
    just_hash_relevant_data = [
        [
            d["image"].image_hash,
            d["boxes"],
        ]
        for d in wrapped_training_data
    ]
    # we dump to pickle to serialize the data as a single object
    return hash_function(pickle.dumps(just_hash_relevant_data))


class OwlV2(RoboflowCoreModel):
    task_type = "object-detection"
    box_format = "xywh"

    def __init__(self, *args, model_id="owlv2/owlv2-large-patch14-ensemble", **kwargs):
        super().__init__(*args, model_id=model_id, **kwargs)
        hf_id = os.path.join("google", self.version_id)
        print(f"Using model: {hf_id}")
        processor = Owlv2Processor.from_pretrained(hf_id)
        self.image_size = tuple(processor.image_processor.size.values())
        self.image_mean = torch.tensor(
            processor.image_processor.image_mean, device=DEVICE
        ).view(1, 3, 1, 1)
        self.image_std = torch.tensor(
            processor.image_processor.image_std, device=DEVICE
        ).view(1, 3, 1, 1)
        self.model = Owlv2ForObjectDetection.from_pretrained(hf_id).eval().to(DEVICE)
        self.reset_cache()

        # compile forward pass of the visual backbone of the model
        # NOTE that this is able to fix the manual attention implementation used in OWLv2
        # so we don't have to force in flash attention by ourselves
        # however that is only true if torch version 2.4 or later is used
        # for torch < 2.4, this is a LOT slower and using flash attention by ourselves is faster
        # this also breaks in torch < 2.1 so we supress torch._dynamo errors
        torch._dynamo.config.suppress_errors = True
        self.model.owlv2.vision_model = torch.compile(self.model.owlv2.vision_model)

    def reset_cache(self):
        # each entry should be on the order of 300*4KB, so 1000 is 400MB of CUDA memory
        self.image_embed_cache = LimitedSizeDict(size_limit=1000)
        # each entry should be on the order of 10 bytes, so 1000 is 10KB
        self.image_size_cache = LimitedSizeDict(size_limit=1000)
        # entry size will vary depending on the number of samples, but 100 should be safe
        self.class_embeddings_cache = LimitedSizeDict(size_limit=100)

    def draw_predictions(
        self,
        inference_request,
        inference_response,
    ) -> bytes:
        """Draw predictions from an inference response onto the original image provided by an inference request

        Args:
            inference_request (ObjectDetectionInferenceRequest): The inference request containing the image on which to draw predictions
            inference_response (ObjectDetectionInferenceResponse): The inference response containing predictions to be drawn

        Returns:
            str: A base64 encoded image string
        """
        all_class_names = [x.class_name for x in inference_response.predictions]
        all_class_names = sorted(list(set(all_class_names)))

        return draw_detection_predictions(
            inference_request=inference_request,
            inference_response=inference_response,
            colors={
                class_name: DEFAULT_COLOR_PALETTE[i % len(DEFAULT_COLOR_PALETTE)]
                for (i, class_name) in enumerate(all_class_names)
            },
        )

    def download_weights(self) -> None:
        # Download from huggingface
        pass

    def compute_image_size(
        self, image: Union[np.ndarray, LazyImageRetrievalWrapper]
    ) -> Tuple[int, int]:
        # we build this in hopes of avoiding having to load the image solely for the purpose of getting its size
        if isinstance(image, LazyImageRetrievalWrapper):
            if (image_size := self.image_size_cache.get(image.image_hash)) is None:
                image_size = image.image_as_numpy.shape[:2][::-1]
                self.image_size_cache[image.image_hash] = image_size
        else:
            image_size = image.shape[:2][::-1]
        return image_size

    @torch.no_grad()
    def embed_image(self, image: Union[np.ndarray, LazyImageRetrievalWrapper]) -> Hash:
        if isinstance(image, LazyImageRetrievalWrapper):
            image_hash = image.image_hash
        else:
            image_hash = hash_function(image.tobytes())

        if image_hash in self.image_embed_cache:
            return image_hash

        np_image = (
            image.image_as_numpy
            if isinstance(image, LazyImageRetrievalWrapper)
            else image
        )
        pixel_values = preprocess_image(
            np_image, self.image_size, self.image_mean, self.image_std
        )

        # torch 2.4 lets you use "cuda:0" as device_type
        # but this crashes in 2.3
        # so we parse DEVICE as a string to make it work in both 2.3 and 2.4
        # as we don't know a priori our torch version
        device_str = "cuda" if str(DEVICE).startswith("cuda") else "cpu"
        # we disable autocast on CPU for stability, although it's possible using bfloat16 would work
        # NOTE: data type actually has a big impact on performance
        with torch.autocast(
            device_type=device_str, dtype=torch.bfloat16, enabled=device_str == "cuda"
        ):
            image_embeds, _ = self.model.image_embedder(pixel_values=pixel_values)
            batch_size, h, w, dim = image_embeds.shape
            image_features = image_embeds.reshape(batch_size, h * w, dim)
            objectness = self.model.objectness_predictor(image_features)
            boxes = self.model.box_predictor(image_features, feature_map=image_embeds)

        image_class_embeds = self.model.class_head.dense0(image_features)
        image_class_embeds /= (
            torch.linalg.norm(image_class_embeds, ord=2, dim=-1, keepdim=True)
        )
        logit_shift = self.model.class_head.logit_shift(image_features)
        logit_scale = (
            self.model.class_head.elu(self.model.class_head.logit_scale(image_features))
            + 1
        )
        objectness = objectness.sigmoid()

        objectness, boxes, image_class_embeds, logit_shift, logit_scale = (
            filter_tensors_by_objectness(
                objectness, boxes, image_class_embeds, logit_shift, logit_scale
            )
        )


        # Convert boxes from center/width/height to corners format
        boxes = to_corners(boxes)

        # Get original aspect ratio to determine image boundaries
        original_h, original_w = np_image.shape[:2]
        max_dim = max(original_h, original_w)
        
        # Scale coordinates based on max dimension
        boxes = boxes * max_dim

        # Filter boxes where top-left is outside image bounds
        valid_tl = (boxes[..., 0] < original_w) & (boxes[..., 1] < original_h)
        boxes = boxes[valid_tl]
        objectness = objectness[valid_tl]
        image_class_embeds = image_class_embeds[valid_tl]
        logit_shift = logit_shift[valid_tl]
        logit_scale = logit_scale[valid_tl]

        # Clip bottom-right coordinates to image bounds
        boxes[..., 2] = boxes[..., 2].clamp(max=original_w)
        boxes[..., 3] = boxes[..., 3].clamp(max=original_h)

        # Normalize back to [0,1] range
        boxes = boxes / max_dim

        # Convert back to center/width/height format
        boxes = from_corners(boxes)

        
        self.image_embed_cache[image_hash] = (
            objectness,
            boxes,
            image_class_embeds,
            logit_shift,
            logit_scale,
        )

        return image_hash

    def get_query_embedding(
        self, query_spec: QuerySpecType, iou_threshold: float, return_missed_embeds: bool = False
    ) -> torch.Tensor:
        # NOTE: for now we're handling each image seperately
        query_embeds = []
        missed_embeds = []
        for image_hash, query_boxes in query_spec.items():
            try:
                _objectness, image_boxes, image_class_embeds, _, _ = (
                    self.image_embed_cache[image_hash]
                )
            except KeyError as error:
                raise KeyError("We didn't embed the image first!") from error

            query_boxes_tensor = torch.tensor(
                query_boxes, dtype=image_boxes.dtype, device=image_boxes.device
            )
            if image_boxes.numel() == 0 or query_boxes_tensor.numel() == 0:
                continue
            iou, _ = box_iou(
                to_corners(image_boxes), to_corners(query_boxes_tensor)
            )  # 3000, k
            ious, indices = torch.max(iou, dim=0)
            # filter for only iou > 0.4
            iou_mask = ious >= iou_threshold
            matched_indices = indices[iou_mask]
            if matched_indices.numel() > 0:
                embeds = image_class_embeds[matched_indices]
                query_embeds.append(embeds)

            if return_missed_embeds:
                missed_indices = torch.ones(image_class_embeds.shape[0], dtype=torch.bool, device=image_class_embeds.device)
                missed_indices[matched_indices] = 0
                missed_indices = torch.nonzero(missed_indices, as_tuple=False)
                if missed_indices.numel() > 0:
                    missed_embeds.append(image_class_embeds[missed_indices].squeeze(1))

        if not query_embeds:
            query = None
        else:
            query = torch.cat(query_embeds, dim=0)

        if return_missed_embeds:
            if not missed_embeds:
                missed_embeds = None
            else:
                missed_embeds = torch.cat(missed_embeds, dim=0)
            return query, missed_embeds
        else:
            return query

    def infer_from_embed(
        self,
        image_hash: Hash,
        query_embeddings: Dict[str, PosNegDictType],
        confidence: float,
        iou_threshold: float,
    ) -> List[Dict]:
        objectness, image_boxes, image_class_embeds, logit_shift, logit_scale = self.image_embed_cache[image_hash]
        class_map, class_names = make_class_map(query_embeddings)
        all_predicted_boxes, all_predicted_classes, all_predicted_scores = [], [], []
        for class_name, pos_neg_embedding_dict in query_embeddings.items():
            boxes, classes, scores = get_class_preds_from_embeds(
                pos_neg_embedding_dict,
                image_class_embeds,
                confidence,
                image_boxes,
                class_map,
                class_name,
                iou_threshold,
                objectness,
                logit_shift,
                logit_scale,
            )

            all_predicted_boxes.append(boxes)
            all_predicted_classes.append(classes)
            all_predicted_scores.append(scores)

        if not all_predicted_boxes:
            return []

        all_predicted_boxes = torch.cat(all_predicted_boxes, dim=0)
        all_predicted_classes = torch.cat(all_predicted_classes, dim=0)
        all_predicted_scores = torch.cat(all_predicted_scores, dim=0)

        # run nms on all predictions
        survival_indices = torchvision.ops.nms(
            to_corners(all_predicted_boxes), all_predicted_scores, iou_threshold
        )
        all_predicted_boxes = all_predicted_boxes[survival_indices]
        all_predicted_classes = all_predicted_classes[survival_indices]
        all_predicted_scores = all_predicted_scores[survival_indices]

        # move tensors to numpy before returning
        all_predicted_boxes = all_predicted_boxes.cpu().numpy()
        all_predicted_classes = all_predicted_classes.cpu().numpy()
        all_predicted_scores = all_predicted_scores.cpu().numpy()

        return [
            {
                "class_name": class_names[int(c)],
                "x": float(x),
                "y": float(y),
                "w": float(w),
                "h": float(h),
                "confidence": float(score),
            }
            for c, (x, y, w, h), score in zip(
                all_predicted_classes, all_predicted_boxes, all_predicted_scores
            )
        ]
    
    def compute_iou_from_training_data_hash(self, training_data_hash: Hash) -> float:
        # we appended the iou threshold to the training data hash
        # so we can use this to compute the iou threshold used for a given training data hash
        return float(training_data_hash.split("-")[-1])

    def infer_from_training_data_hash(
        self,
        image: Any,
        training_data_hash: Hash,
        confidence: float,
        **kwargs,
    ):
        iou_threshold = self.compute_iou_from_training_data_hash(training_data_hash)
        class_embeddings_dict = self.class_embeddings_cache[training_data_hash]

        if not isinstance(image, list):
            images = [image]
        else:
            images = image

        images = [LazyImageRetrievalWrapper(image) for image in images]

        results = []
        image_sizes = []
        for image_wrapper in images:
            # happy path here is that both image size and image embeddings are cached
            # in which case we avoid loading the image at all
            image_size = self.compute_image_size(image_wrapper)
            image_sizes.append(image_size)
            image_hash = self.embed_image(image_wrapper)
            result = self.infer_from_embed(
                image_hash, class_embeddings_dict, confidence, iou_threshold
            )
            results.append(result)
        return self.make_response(
            results, image_sizes, sorted(list(class_embeddings_dict.keys()))
        )

    def infer_via_head(
        self,
        image: Any,
        head: torch.nn.Module,
        class_names: List[str],
        iou_threshold: float = 0.3,
        **kwargs,
    ):
        if not isinstance(image, list):
            images = [image]
        else:
            images = image

        images = [LazyImageRetrievalWrapper(image) for image in images]

        results = []
        image_sizes = []
        for image_wrapper in images:
            this_image_results = []

            image_size = self.compute_image_size(image_wrapper)
            image_sizes.append(image_size)
            image_hash = self.embed_image(image_wrapper)

            _, boxes, image_class_embeds, _, _ = self.image_embed_cache[image_hash]

            boxes = boxes.squeeze(0).float()
            image_class_embeds = image_class_embeds.squeeze(0).float()

            print(f"shape of image_class_embeds: {image_class_embeds.shape}")
            print(f"shape of boxes: {boxes.shape}")

            class_logits = head(image_class_embeds).softmax(dim=-1)
            class_inds = torch.argmax(class_logits, dim=-1)
            classes = [class_names[i] for i in class_inds]
            scores = class_logits[torch.arange(class_logits.shape[0]), class_inds]

            corners = to_corners(boxes)
            survival_indices = torchvision.ops.nms(corners, scores, iou_threshold)
            boxes = boxes[survival_indices]
            classes = [classes[i] for i in survival_indices]
            scores = [scores[i] for i in survival_indices]

            for i in range(len(boxes)):
                if classes[i] != "_background":
                    this_image_results.append({
                        "class_name": classes[i],
                        "x": boxes[i][0],
                        "y": boxes[i][1],
                        "w": boxes[i][2],
                        "h": boxes[i][3],
                        "confidence": scores[i],
                    })
            results.append(this_image_results)
        return self.make_response(results, image_sizes, class_names)

    def infer(
        self,
        image: Any,
        training_data: Dict,
        confidence=0.99,
        iou_threshold=0.3,
        **kwargs,
    ):
        training_data_hash = self.embed_training_data(training_data, iou_threshold)
        return self.infer_from_training_data_hash(image, training_data_hash, confidence, iou_threshold)

    def make_class_embeddings_dict(
        self, training_data: List[Any], iou_threshold: float
    ) -> Dict[str, PosNegDictType]:
        wrapped_training_data_hash = self.embed_training_data(
            training_data, iou_threshold
        )
        return self.class_embeddings_cache[wrapped_training_data_hash]

    def embed_training_data(
        self, training_data: List[Any], iou_threshold: float
    ) -> Hash:
        wrapped_training_data = [
            {
                "image": LazyImageRetrievalWrapper(train_image["image"]),
                "boxes": train_image["boxes"],
            }
            for train_image in training_data
        ]

        # NOTE: this should take into account the order of the training data
        wrapped_training_data_hash = hash_wrapped_training_data(wrapped_training_data)
        # make sure we include the iou threshold in the hash since different thresholds yield different embeddings
        wrapped_training_data_hash += f"-{iou_threshold}"
        if (
            class_embeddings_dict := self.class_embeddings_cache.get(
                wrapped_training_data_hash
            )
        ) is not None:
            return wrapped_training_data_hash

        class_embeddings_dict = defaultdict(lambda: {"positive": [], "negative": []})

        total_boxes = 0
        total_matches = 0

        bool_to_literal = {True: "positive", False: "negative"}
        for train_image in wrapped_training_data:
            # grab and embed image
            image_hash = self.embed_image(train_image["image"])

            # grab and normalize box prompts for this image
            image_size = self.compute_image_size(train_image["image"])
            boxes = train_image["boxes"]
            coords = [[box["x"], box["y"], box["w"], box["h"]] for box in boxes]
            coords = [tuple([c / max(image_size) for c in coord]) for coord in coords]
            classes = [box["cls"] for box in boxes]
            is_positive = [not box["negative"] for box in boxes]

            total_boxes += len(boxes)

            # compute the embeddings for the box prompts
            query_spec = {image_hash: coords}
            # NOTE: because we just computed the embedding for this image, this should never result in a KeyError
            embeddings = self.get_query_embedding(query_spec, iou_threshold)

            if embeddings is None:
                continue

            total_matches += embeddings.shape[0]

            # add the embeddings to their appropriate class and positive/negative list
            for embedding, class_name, is_positive in zip(
                embeddings, classes, is_positive
            ):
                class_embeddings_dict[class_name][bool_to_literal[is_positive]].append(
                    embedding
                )

        # convert the lists of embeddings to tensors

        # print(f"total boxes: {total_boxes}, total matches: {total_matches}")

        class_embeddings_dict = {
            k: {
                "positive": torch.stack(v["positive"]) if v["positive"] else None,
                "negative": torch.stack(v["negative"]) if v["negative"] else None,
            }
            for k, v in class_embeddings_dict.items()
        }

        self.class_embeddings_cache[wrapped_training_data_hash] = class_embeddings_dict

        return wrapped_training_data_hash
    
    def mine_training_data(self, training_data: List[Any], iou_threshold: float) -> Tuple[Dict[str, torch.Tensor], torch.Tensor]:
        # we embed the training data and return query hits as positive and non-hits as negative
        # we would then use this to train a new class head for the model
        # Initialize defaultdict to store embeddings for each class
        class_examples = defaultdict(list)
        all_misses = []

        # Process each training example
        for train_image in training_data:
            # Embed the image
            image_hash = self.embed_image(train_image["image"])

            # Get image size and normalize box coordinates
            image_size = self.compute_image_size(train_image["image"])
            boxes = train_image["boxes"]
            coords = [[box["x"], box["y"], box["w"], box["h"]] for box in boxes]
            coords = [tuple([c / max(image_size) for c in coord]) for coord in coords]
            classes = [box["cls"] for box in boxes]

            # Get embeddings for the boxes
            query_spec = {image_hash: coords}
            query_embeds, missed_embeds = self.get_query_embedding(
                query_spec, iou_threshold, return_missed_embeds=True
            )

            num_matches = query_embeds.shape[0] if query_embeds is not None else 0
            num_misses = missed_embeds.shape[0] if missed_embeds is not None else 0

            # Handle matched embeddings (positives)
            if query_embeds is not None:
                for embedding, class_name in zip(query_embeds, classes):
                    class_examples[class_name].append(embedding)

            # Handle missed embeddings (negatives)
            if missed_embeds is not None:
                all_misses.append(missed_embeds)

        # Convert lists to tensors
        class_examples = {
            k: torch.stack(v) for k, v in class_examples.items()
        }

        return class_examples, torch.cat(all_misses, dim=0)

    def make_response(self, predictions, image_sizes, class_names):
        responses = [
            ObjectDetectionInferenceResponse(
                predictions=[
                    ObjectDetectionPrediction(
                        # Passing args as a dictionary here since one of the args is 'class' (a protected term in Python)
                        **{
                            "x": pred["x"] * max(image_sizes[ind]),
                            "y": pred["y"] * max(image_sizes[ind]),
                            "width": pred["w"] * max(image_sizes[ind]),
                            "height": pred["h"] * max(image_sizes[ind]),
                            "confidence": pred["confidence"],
                            "class": pred["class_name"],
                            "class_id": class_names.index(pred["class_name"]),
                        }
                    )
                    for pred in batch_predictions
                ],
                image=InferenceResponseImage(
                    width=image_sizes[ind][0], height=image_sizes[ind][1]
                ),
            )
            for ind, batch_predictions in enumerate(predictions)
        ]
        return responses

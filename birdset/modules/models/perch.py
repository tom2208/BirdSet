from typing import Optional, Tuple
import warnings

import numpy as np
import pandas as pd
import tensorflow as tf
import tensorflow_hub as hub
import torch
from torch import nn

from utils import get_label_to_class_mapping_from_metadata

class PerchModel(nn.Module):
    """
    A PyTorch model for bird vocalization classification, integrating a TensorFlow Hub model.

    Attributes:
        PERCH_TF_HUB_URL (str): URL to the TensorFlow Hub model for bird vocalization.
        EMBEDDING_SIZE (int): The size of the embeddings produced by the TensorFlow Hub model.
        num_classes (int): The number of classes to classify into.
        tfhub_version (str): The version of the TensorFlow Hub model to use.
        train_classifier (bool): Whether to train a classifier on top of the embeddings.
        restrict_logits (bool): Whether to restrict output logits to target classes only.
        dataset_info_path (Optional[str]): Path to the dataset information file for target class filtering.
        task (Optional[str]): The type of classification task ('multiclass' or 'multilabel').
        model: The loaded TensorFlow Hub model (loaded dynamically).
        class_mapping (Optional[np.ndarray]): Classes from the TensorFlow Hub model.
        classifier (Optional[nn.Linear]): A linear classifier layer on top of the embeddings.
    """

    # Constants for the model URL and embedding size
    PERCH_TF_HUB_URL = "https://tfhub.dev/google/bird-vocalization-classifier"
    EMBEDDING_SIZE = 1280

    def __init__(
        self,
        num_classes: int,
        tfhub_version: str,
        label_path: str,
        train_classifier: bool,
        restrict_logits: bool = False,
        dataset_info_path: Optional[str] = None,
        task: Optional[str] = None,
    ) -> None:
        """
        Initializes the PerchModel with configuration for loading the TensorFlow Hub model,
        an optional classifier, and setup for target class restriction based on dataset info.

        Args:
            num_classes: The number of output classes for the classifier.
            tfhub_version: The version identifier of the TensorFlow Hub model to load.
            label_path: Path to a CSV file containing the class information for the Perch model.
            train_classifier: If True, a classifier is added on top of the model embeddings.
            restrict_logits: If True, output logits are restricted to target classes based on dataset info.
            dataset_info_path: Optional path to a JSON file containing target class information.
            task: The classification task type ('multiclass' or 'multilabel'), used with `dataset_info_path`.
        """
        super().__init__()
        self.model = None  # Placeholder for the loaded model
        self.class_mapping = None

        self.train_classifier = train_classifier
        self.restrict_logits = restrict_logits
        self.dataset_info_path = dataset_info_path
        self.label_path = label_path
        self.task = task
        self.target_indices = None

        self.num_classes = num_classes
        self.tfhub_version = tfhub_version
        # Define a linear classifier to use on top of the embeddings

        # self.classifier = nn.Linear(
        #     in_features=self.EMBEDDING_SIZE, out_features=num_classes
        # )
        self.classifier = nn.Sequential(
            nn.Linear(self.EMBEDDING_SIZE, 128), 
            nn.ReLU(),                           
            nn.Dropout(0.5),                    
            nn.Linear(128, 64),                 
            nn.ReLU(),                           
            nn.Linear(64, self.num_classes)           
        )

        self.load_model()

    def load_model(self) -> None:
        """
        Load the model from TensorFlow Hub.
        """
        model_url = f"{self.PERCH_TF_HUB_URL}/{self.tfhub_version}"
        #self.model = hub.load(model_url)
        # with tf.device('/CPU:0'):
        #     model = hub.load(model_url)

        physical_devices = tf.config.list_physical_devices('GPU')
        tf.config.experimental.set_memory_growth(physical_devices[0], True)
        tf.config.optimizer.set_jit(True)
        self.model = hub.load(model_url)

        # Load the class list from the CSV file
        class_mapping_df = pd.read_csv(self.label_path)
        # Extract the 'ebird2021' column as a numpy array
        class_mapping = class_mapping_df["ebird2021"].values
        # Convert the class mapping to a dictionary {index: "label"}
        self.class_mapping = dict(enumerate(class_mapping))

        if self.restrict_logits:
            self.target_indices = self.restrict_logits_to_target_classes()

    @tf.function  # Decorate with tf.function to compile into a callable TensorFlow graph
    def run_tf_model(self, input_tensor: tf.Tensor) -> dict:
        """
        Run the TensorFlow model and get outputs.

        Args:
            input_tensor (tf.Tensor): The input tensor for the model.

        Returns:
            dict: A dictionary of model outputs.
        """

        return self.model.signatures["serving_default"](inputs=input_tensor)

    def forward(
        self, input_values: torch.Tensor, labels: Optional[torch.Tensor] = None
    ) -> torch.Tensor:
        """
        Forward pass through the model.

        Args:
            input_values (torch.Tensor): The input tensor for the classifier.
            labels (Optional[torch.Tensor]): The true labels for the input values. Default is None.

        Returns:
            torch.Tensor: The output of the classifier.
        """
        # If there's an extra channel dimension, remove it
        if input_values.dim() > 2:
            input_values = input_values.squeeze(1)

        device = input_values.device  # Get the device of the input tensor

        # Move the tensor to the CPU and convert it to a NumPy array.
        input_values = input_values.cpu().numpy()

        # Get embeddings from the Perch model and move to the same device as input_values
        embeddings, logits = self.get_embeddings(input_tensor=input_values)

        if self.train_classifier:
            embeddings = embeddings.to(device)
            # Pass embeddings through the classifier to get the final output
            output = self.classifier(embeddings)
        else:
            output = logits.to(device)

        return output

    def get_embeddings(
        self, input_tensor: tf.Tensor
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Get the embeddings and logits from the Perch model.

        Args:
            input_tensor (tf.Tensor): The input tensor for the model.

        Returns:
            Tuple[torch.Tensor, torch.Tensor]: A tuple of two tensors (embeddings, logits).
        """

        # Normalize the input tensor
        input_tensor = self.normalize_audio(input_tensor)

        input_tensor = input_tensor.reshape([-1, input_tensor.shape[-1]])

        # Run the model and get the outputs using the optimized TensorFlow function
        outputs = self.run_tf_model(input_tensor=input_tensor)

        # Extract embeddings and logits, convert them to PyTorch tensors
        embeddings = torch.from_numpy(outputs["output_1"].numpy())
        logits = torch.from_numpy(outputs["output_0"].numpy())

        if self.restrict_logits:
            logits = logits[:, self.target_indices]

        return embeddings, logits

    def restrict_logits_to_target_classes(self) -> list[int]:
        """
        Restricts and reorders the logits to only include those corresponding to target classes.

        This method identifies the indices of the logits produced by the model that correspond to
        the target classes specified in the dataset information file. It uses these indices to filter
        and reorder any tensor of logits according to the target classes.

        Returns:
            list[int]: Indices of logits that correspond only to the target classes,
                       in the order specified by the target class list.

        Raises:
            FileNotFoundError: If the dataset information file does not exist.
            KeyError: If the expected keys are not found in the dataset information JSON structure.
            ValueError: If a target class from the mapping is not found in `self.class_mapping`.
        """
        # Load the mapping from file_path and task, creating a label to eBird code mapping.
        label_to_class_mapping = get_label_to_class_mapping_from_metadata(
            file_path=self.dataset_info_path, task=self.task
        )

        # Reverse the mapping for easier access to labels by eBird code.
        class_to_label_mapping = {v: k for k, v in label_to_class_mapping.items()}

        # Invert `self.class_mapping` to get a "label to index" mapping for direct lookup.
        label_to_index_mapping = {v: k for k, v in self.class_mapping.items()}

        # Initialize an empty list to store indices of logits corresponding to target classes.
        target_indices: list[int] = []

        # Identify indices of `self.class_mapping` that match the target classes in the mapping.
        for target_class in class_to_label_mapping.keys():
            if target_class in label_to_index_mapping:
                idx = label_to_index_mapping[target_class]
                target_indices.append(idx)
            else:
                warnings.warn(f"Target class {target_class} not found in class_mapping.")

        return target_indices

    def normalize_audio(
        self,
        framed_audio: np.ndarray,
        target_peak: float = 0.25,
    ) -> np.ndarray:
        """Normalizes audio with shape [..., T] to match the target_peak value."""
        framed_audio = framed_audio.copy()
        framed_audio -= np.mean(framed_audio, axis=-1, keepdims=True)
        peak_norm = np.max(np.abs(framed_audio), axis=-1, keepdims=True)
        framed_audio = np.divide(framed_audio, peak_norm, where=(peak_norm > 0.0))
        framed_audio = framed_audio * target_peak
        return framed_audio
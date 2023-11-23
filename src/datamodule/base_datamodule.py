import logging
import os

import hydra
import lightning as L

from datasets import load_dataset, load_from_disk, Audio, DatasetDict
from torch.utils.data import DataLoader
from omegaconf import DictConfig

from src.datamodule.components.bird_premapping import AudioPreprocessor
from src.datamodule.components.transforms import TransformsWrapperN
from src.datamodule.components.transforms import TransformsWrapper

class BaseDataModuleHF(L.LightningDataModule):
    """
    A base data module for handling datasets using Hugging Face's datasets library.
    """

    def __init__(
        self, 
        dataset: DictConfig, 
        loaders: DictConfig, 
        transforms: DictConfig,
        extractors: DictConfig,
    ):
        """
        Constructs all the necessary attributes for the BaseDataModuleHF object.

        Parameters
        ----------
            dataset : DictConfig
                The configuration for the dataset.
            loaders : DictConfig
                The configuration for the data loaders.
            transforms : DictConfig
                The configuration for the data transformations.
            extractors : DictConfig
                The configuration for the feature extractors.
            transforms_rene : DictConfig
                The configuration for the Rene's transformations.
        """
        super().__init__()
        self.dataset = dataset
        self.loaders = loaders
        self.transforms = TransformsWrapperN(transforms)
        self.feature_extractor = hydra.utils.instantiate(extractors)

        self.data_path = None
        self.train_dataset = None
        self.val_dataset = None
        self.test_dataset = None

        self._prepare_done = False
        self._setup_done = False
        self.data_path = None
        self.len_trainset = None

    def _create_splits(self, dataset):
        logging.info("> Creating Splits.")
        split = dataset["train"].train_test_split(
            self.dataset.val_split, shuffle=True, seed=self.dataset.seed
        )
        train_dataset = split["train"]
        val_dataset = split["test"]
        test_dataset = dataset["test"]
        return train_dataset, val_dataset, test_dataset

    # prepare data is
    def prepare_data(self):
        """
        Prepares the data for use.

        This method checks if the data preparation has already been done. If not, it loads the dataset, applies transformations,
        creates train, validation, and test splits, and saves the processed data to disk. If the data has already been prepared,
        this method does nothing.

        The method supports both multilabel and multiclass tasks. For multilabel tasks, it selects a subset of the data,
        applies preprocessing, and selects the necessary columns. For multiclass tasks, it applies preprocessing and selects
        the necessary columns.

        If the feature extractor is configured to return an attention mask, this method adds 'attention_mask' to the list of
        columns to select from the dataset.

        The method saves the processed dataset to disk in the directory specified by the 'data_dir' attribute of the 'dataset'
        configuration, under a subdirectory named after the dataset and the fingerprint of the training data.

        After the data is prepared, this method sets the '_prepare_done' attribute to True and the 'len_trainset' attribute
        to the length of the training dataset.
        """

        logging.info("Check if preparing has already been done.")

        if self._prepare_done:
            logging.info("Skip preparing.")
            return

        logging.info("> Loading data set.")

        dataset = load_dataset(
            name=self.dataset.hf_name,
            path=self.dataset.hf_path,
            cache_dir=self.dataset.data_dir,
            num_proc=3,
        )

        dataset = dataset.cast_column(
            column="audio",
            feature=Audio(
                sampling_rate=self.feature_extractor.sampling_rate,
                mono=True,
                decode=True,
            ),
        )

        logging.info("> Mapping data set.")

        preprocessor = AudioPreprocessor(
            feature_extractor=self.feature_extractor,
            n_classes=self.dataset.n_classes,
            window_length=5,
        )

        if self.dataset.task == "multilabel":
            # TODO: remove this
            dataset["test_5s"] = self._preprocess_multilabel(dataset, "test_5s", preprocessor, range(1000))
            dataset["train"] = self._preprocess_multilabel(dataset, "train", preprocessor, range(1000))
            dataset = DatasetDict(dict(list(dataset.items())[:2]))

        elif self.dataset.task == "multiclass":
            dataset = DatasetDict(dict(list(dataset.items())[:2]))
            dataset = dataset.map(
                preprocessor.preprocess_train,
                remove_columns=["audio"],
                batched=True,
                batch_size=100,
                load_from_cache_file=True,
                num_proc=self.dataset.n_workers,
            )

            dataset = dataset.select_columns(self.dataset.column_list)

            if self.dataset.column_list[1] != "labels":
                dataset = dataset.rename_column(self.dataset.column_list[1], "labels")

        if self.feature_extractor.return_attention_mask:
            self.dataset.column_list.append("attention_mask")

        dataset.set_format("np")
        train_dataset, val_dataset, test_dataset = self._create_splits(dataset)
        complete = DatasetDict(
            {"train": train_dataset, "valid": val_dataset, "test": test_dataset}
        )
        data_path = os.path.join(
            self.dataset.data_dir,
            f"{self.dataset.dataset_name}_processed",
            train_dataset._fingerprint,
        )
        self.data_path = data_path
        self._prepare_done = True
        self.len_trainset = len(train_dataset)

        if os.path.exists(data_path):
            logging.info("Dataset exists on disk.")
            return

        logging.info(f"Saving to disk: {os.path.join(self.data_path)}")
        complete.save_to_disk(self.data_path)

    def _preprocess_multilabel(self, dataset, split, preprocessor, select_range=None):
        """
        Preprocesses a multilabel dataset.

        Parameters
        ----------
        dataset : DatasetDict
            The dataset to preprocess.
        split : str
            The split of the dataset to preprocess.
        preprocessor : Preprocessor
            The preprocessor to use.
        select_range : range, optional
            A range of indices to select from the dataset before preprocessing.

        Returns
        -------
        Dataset
            The preprocessed dataset.
        """
        if select_range is not None:
            dataset[split] = dataset[split].select(select_range)
        dataset[split] = dataset[split].map(
            preprocessor.preprocess_multilabel,
            remove_columns=["audio"],
            batched=True,
            batch_size=100,
            load_from_cache_file=True,
            num_proc=1,
            # TODO: make num_proc configurable num_proc=self.dataset.n_workers,
        )
        dataset[split] = dataset[split].select_columns(["input_values", "labels"])
        return dataset[split]

    def _get_dataset(self, split):
        dataset = load_from_disk(
            os.path.join(self.data_path, split)
        )
        self.transforms.set_mode(split)
        dataset.set_transform(self.transforms, output_all_columns=False) 
        
        return dataset

    def setup(self, stage=None):
        if not self.train_dataset and not self.val_dataset:
            if stage == "fit":
                logging.info("fit")
                self.train_dataset = self._get_dataset("train")
                self.val_dataset = self._get_dataset("valid")

        if not self.test_dataset:
            if stage == "test":
                logging.info("test")
                self.test_dataset = self._get_dataset("test")

    def train_dataloader(self):
        # TODO: nontype objects in hf dataset
        return DataLoader(self.train_dataset, **self.loaders.get("train"))

    def val_dataloader(self):
        return DataLoader(self.val_dataset, **self.loaders.get("valid"))

    def test_dataloader(self):
        return DataLoader(self.test_dataset, **self.loaders.get("test"))

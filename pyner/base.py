import random
import time
import warnings
from collections import defaultdict
from itertools import chain

import pytorch_lightning as pl
import transformers
from importlib import import_module

from pyner.data_utils import loop, mappable, batchify
from pyner.optimization import *
from pyner.registry import register, get_instance, get_config
from pyner.torch_utils import fork_rng, identity

import torch


class DummyIterableDataset(torch.utils.data.IterableDataset):
    def __init__(self, data, epoch_length=None):
        super().__init__()
        self.data = iter(data)
        self.epoch_length = epoch_length
        warnings.filterwarnings('ignore', "Your `IterableDataset` has `__len__` defined")

    def __iter__(self):
        return self.data

    def __len__(self):
        if self.epoch_length is not None:
            return self.epoch_length
        raise TypeError()


class PytorchLightningBase(pl.LightningModule):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        warnings.filterwarnings("ignore", ".*does not have many workers which may be a bottleneck.*")

    @property
    def train_dataloader(self):
        def fn():
            if getattr(self, 'train_data', None) is None:
                return None
            with fork_rng(self.data_seed):
                non_default_epoch_length = (
                    self.trainer.val_check_interval * self.batch_size
                    if (getattr(self, 'trainer', None) is not None
                        and self.trainer.val_check_interval is not None
                        and self.trainer.max_steps is not None) else None
                )
                if hasattr(self.train_data, '__getitem__') and non_default_epoch_length is None:
                    prep = self.preprocess(
                        self.train_data,
                        split="train"
                    )
                    return torch.utils.data.DataLoader(prep, shuffle=True, batch_size=self.batch_size, collate_fn=identity)
                elif non_default_epoch_length is not None and hasattr(self.train_data, '__len__'):
                    if self.dynamic_preprocessing:
                        prep = self.preprocess(loop(self.train_data, shuffle=True), split="train")
                    else:
                        prep = loop(self.preprocess(self.train_data, split="train"), shuffle=True)

                    return torch.utils.data.DataLoader(
                        DummyIterableDataset(prep, epoch_length=non_default_epoch_length),
                        shuffle=False, batch_size=self.batch_size, collate_fn=identity)
                else:
                    prep = self.preprocess(
                        self.train_data,
                        split="train"
                    )
                    return torch.utils.data.DataLoader(
                        DummyIterableDataset(prep, epoch_length=non_default_epoch_length),
                        shuffle=False, batch_size=self.batch_size, collate_fn=identity)

        return fn

    def transfer_batch_to_device(self, inputs, device):
        return inputs

    @property
    def val_dataloader(self):
        def fn():
            if getattr(self, 'val_data', None) is None or len(self.val_data) == 0:
                return None
            with fork_rng(self.data_seed):
                prep = self.preprocess(self.val_data, split="val")
                if hasattr(prep, '__getitem__'):
                    return torch.utils.data.DataLoader(prep, shuffle=False, batch_size=self.batch_size, collate_fn=identity)
                else:
                    return torch.utils.data.DataLoader(
                        DummyIterableDataset(prep, None),
                        shuffle=False, batch_size=self.batch_size, collate_fn=identity)

        return fn

    @property
    def test_dataloader(self):
        def fn():
            if getattr(self, 'test_data', None) is None or len(self.test_data) == 0:
                return None
            with fork_rng(self.data_seed):
                prep = self.preprocess(self.test_data, split="test")
                batch_size = self.batch_size
                if hasattr(prep, '__getitem__'):
                    return torch.utils.data.DataLoader(prep, shuffle=False, batch_size=batch_size, collate_fn=identity)
                else:
                    return torch.utils.data.DataLoader(
                        DummyIterableDataset(prep, None),
                        shuffle=False, batch_size=batch_size, collate_fn=identity)

        return fn

    @train_dataloader.setter
    def train_dataloader(self, data):
        self.train_data = data()
        if hasattr(self.train_data, 'dataset'):
            self.train_data = self.train_data.dataset

    @val_dataloader.setter
    def val_dataloader(self, data):
        self.val_data = data()
        if hasattr(self.val_data, 'dataset'):
            self.val_data = self.val_data.dataset

    @test_dataloader.setter
    def test_dataloader(self, data):
        self.test_data = data()
        if hasattr(self.test_data, 'dataset'):
            self.test_data = self.test_data.dataset


def save_pretrained(self, filename):
    config = get_config(self)
    torch.save({"config": config, "state_dict": self.state_dict()}, filename)


def load_pretrained(path, map_location=None):
    loaded = torch.load(path, map_location=map_location)
    instance = get_instance(loaded["config"])
    instance.load_state_dict(loaded["state_dict"], strict=False)
    instance.eval()
    return instance


@register("ie")
class InformationExtractor(PytorchLightningBase):
    def __init__(
          self,
          preprocessor,
          encoder,
          decoder,
          seed=42,
          data_seed=None,

          batch_size=24,
          fast_lr=1.5e-3,
          main_lr=1.5e-3,
          bert_lr=4e-5,
          gradient_clip_val=5.,
          metrics=None,
          warmup_rate=0.1,
          use_lr_schedules=True,
          dynamic_preprocessing=False,
          size_factor=20,
          optimizer_cls=torch.optim.AdamW,
    ):
        """

        :param preprocessor: dict
            Preprocessor module parameters
        :param encoder: dict or list of dict
            Word encoders module parameters
        :param decoder: dict
            Decoder module parameters
        :param seed: int
            Seed for the model weights
        :param data_seed: int
            Seed for the data shuffling
        :param batch_size: int
            Batch size
        :param fast_lr: float
            Top modules parameters' learning rate, typically higher than other parameters learning rates
        :param main_lr: float
            Intermediate modules parameters' learning rate
        :param bert_lr: float
            BERT modules parameters' learning rate
        :param gradient_clip_val:
            Use gradient clipping
        :param warmup_rate: float
            Apply warmup for how much of the training (defaults to 0.1 = 10%)
        :param use_lr_schedules: bool
            Use learning rate schedules
        :param optimizer_cls: str or type
            Torch optimizer class to use
        """
        super().__init__()

        # monkey_patch()

        self.automatic_optimization = False
        if data_seed is None:
            data_seed = seed
        self.seed = seed
        self.data_seed = data_seed

        self.size_factor = size_factor
        self.gradient_clip_val = gradient_clip_val
        self.fast_lr = fast_lr
        self.main_lr = main_lr
        self.bert_lr = bert_lr
        self.use_lr_schedules = use_lr_schedules
        self.warmup_rate = warmup_rate
        self.batch_size = batch_size
        self.optimizer_cls = getattr(import_module(optimizer_cls.rsplit(".", 1)[0]), optimizer_cls.rsplit(".", 1)[1]) if isinstance(optimizer_cls, str) else optimizer_cls

        self.dynamic_preprocessing = dynamic_preprocessing
        self.preprocessor = get_instance(preprocessor)

        if metrics is None:
            metrics = {
                "exact": dict(module="dem", binarize_tag_threshold=1., binarize_label_threshold=1.),
                "approx": dict(module="dem", binarize_tag_threshold=False, binarize_label_threshold=1.),
            }
        self.metrics = torch.nn.ModuleDict({k: get_instance(m) for k, m in metrics.items()})

        # Init postponed to setup
        self.encoder = encoder
        self.decoder = decoder

        if not any(voc.training for voc in self.preprocessor.vocabularies.values()):
            self.init_modules()
        self.counter = 0
        self._time = time.time()

    def init_modules(self):
        # Init modules that depend on the vocabulary
        with fork_rng(self.seed):
            with fork_rng(True):
                self.encoder = get_instance({**self.encoder, "_preprocessor": self.preprocessor}) if not isinstance(self.encoder, torch.nn.Module) else self.encoder
            with fork_rng(True):
                self.decoder = get_instance({**self.decoder, "_preprocessor": self.preprocessor, "_encoder": self.encoder}) if not isinstance(self.decoder, torch.nn.Module) else self.decoder

    def setup(self, stage='fit'):
        if stage == 'fit':
            if any(voc.training for voc in self.preprocessor.vocabularies.values()):
                for sample in self.train_dataloader():
                    pass

                self.preprocessor.vocabularies.eval()
                self.init_modules()

            config = get_config(self, drop_unserialized_keys=True)
            self.hparams = config
            self.trainer.gradient_clip_val = self.gradient_clip_val
            self.logger.log_hyperparams(self.hparams)

    def preprocess(self, data, split='train'):
        def shuffle(x):
            x = list(x)
            random.shuffle(x)
            return x

        if split == "train" and self.dynamic_preprocessing:
            data = self.preprocessor(data, only_text=False, chain=True)
            return chain.from_iterable(map(shuffle, batchify(data, 1000)))
        else:
            training = self.preprocessor.training
            self.preprocessor.eval()
            data = list(self.preprocessor(data, only_text=False, chain=True))
            self.preprocessor.training = training
            return data

    def split_into_mini_batches_to_fit_memory(self, samples):
        device_index = next(self.parameters()).device.index
        if device_index is None:
            max_memory = 20000
        else:
            max_memory = min(torch.cuda.get_device_properties(device_index).total_memory / 1024 ** 2, 25000)
        # bert_size_factor = size_factor#50 if self.encoder.bert.config.num_hidden_layers > 12 or self.encoder.bert.config.hidden_size > 768 else 5
        threshold = max_memory / self.size_factor

        samples_and_sizes = sorted([(sample, len(sample["tokens_mask"]), max((len(x) for x in sample["tokens_mask"])))
                                    for sample in samples], key=lambda x: x[1] * x[2])

        max_sequence_size_so_far = 0
        n_sequences_so_far = 0
        batch = [samples_and_sizes[0][0]]
        for sample, n_sequences, max_sequence_size in samples_and_sizes[1:]:
            max_sequence_size_so_far = max(max_sequence_size_so_far, max_sequence_size)
            n_sequences_so_far += n_sequences
            if n_sequences_so_far * max_sequence_size_so_far > threshold:
                max_sequence_size_so_far = max_sequence_size
                n_sequences_so_far = n_sequences
                yield batch
                batch = [sample]
            else:
                batch.append(sample)
        if len(batch):
            yield batch

    def forward(self, inputs, return_loss=False, return_predictions=True, group_by_document=False, **kwargs):
        self.last_inputs = inputs
        device = next(self.parameters()).device
        input_tensors = self.preprocessor.tensorize(inputs, device=device)
        embeds = self.encoder(input_tensors)
        results = self.decoder(embeds, input_tensors, return_loss=return_loss, return_predictions=return_predictions, **kwargs)
        if return_predictions:
            results['predictions'] = self.preprocessor.decode(results['predictions'], inputs, group_by_document=group_by_document)
        return results

    def transfer_batch_to_device(self, inputs, device):
        return inputs

    def training_step(self, inputs, batch_idx):
        self.zero_grad()
        losses = defaultdict(lambda: 0)
        for mini_batch in self.split_into_mini_batches_to_fit_memory(inputs):
            outputs = self(mini_batch, return_loss=True, return_predictions=False)
            key = value = None
            for key, value in outputs.items():
                if key.endswith("loss"):
                    losses[key] += float(value)
            if any(p.grad.isnan().any() for p in self.parameters() if p.grad is not None):
                raise Exception()
            (outputs['loss']/len(inputs)).backward()
            del outputs, key, value

        self.counter += 1
        self.decoder.on_training_step(self.counter, self.max_steps)
        self.trainer.train_loop.track_and_norm_grad(self.optimizers())
        self.optimizers().step()
        return {**losses, "count": len(inputs)}

    def on_train_epoch_start(self):
        self._time = time.time()

    def training_epoch_end(self, outputs):
        total = sum(output["count"] for output in outputs)
        for key in outputs[0].keys():
            if key.endswith("loss"):
                self.log(key, sum(output[key] * output["count"] for output in outputs) / total)
        self.log("main_lr", self.optimizers().param_groups[0]["lr"])
        self.log("fast_lr", self.optimizers().param_groups[1]["lr"])
        self.log("bert_lr", self.optimizers().param_groups[2]["lr"])
        self.log("duration", time.time() - self._time)

    def validation_step(self, inputs, batch_idx):
        for mini_batch in self.split_into_mini_batches_to_fit_memory(inputs):
            outputs = self(mini_batch, return_loss=False, return_predictions=True)
            predictions = outputs['predictions']
            for metric in self.metrics.values():
                metric(predictions, [s["original_sample"] for s in mini_batch])
        return {"count": len(inputs)}

    def validation_epoch_end(self, outputs):
        self.log_dict({
            ("val_{}_{}".format(name, field) if field else "val_{}".format(name, )): value
            for name, metric in self.metrics.items()
            for field, value in metric.compute().items()
        })

    test_step = validation_step

    def test_epoch_end(self, outputs):
        self.log_dict({
            ("test_{}_{}".format(name, field) if field else "test_{}".format(name, )): value
            for name, metric in self.metrics.items()
            for field, value in metric.compute().items()
        })

    def configure_optimizers(self):
        bert_params = list(chain.from_iterable([m.parameters() for m in self.modules() if isinstance(m, transformers.PreTrainedModel)]))
        fast_params = self.decoder.fast_params()
        main_params = [p for p in self.parameters() if not any(p is q for q in bert_params) and not any(p is q for q in fast_params)]
        if self.trainer.max_steps is not None:
            max_steps = self.max_steps = self.trainer.max_steps
        else:
            max_steps = self.max_steps = self.trainer.max_epochs * len(self.train_dataloader())
        for lr, params in [(self.main_lr, main_params), (self.fast_lr, fast_params), (self.bert_lr, bert_params)]:
            if lr == 0:
                print("Some parameters are optimized with a learning rate of 0 => freezing them")
                for param in params:
                    param.requires_grad = False
        optimizer = ScheduledOptimizer(self.optimizer_cls([
            {"params": main_params,
             "lr": self.main_lr,
             "schedules": LinearSchedule(path="lr", warmup_rate=0, total_steps=max_steps) if self.use_lr_schedules else []},
            {"params": fast_params,
             "lr": self.fast_lr,
             "schedules": LinearSchedule(path="lr", warmup_rate=0, total_steps=max_steps) if self.use_lr_schedules else []},
            {"params": bert_params,
             "lr": self.bert_lr,
             "schedules": LinearSchedule(path="lr", warmup_rate=self.warmup_rate, total_steps=max_steps) if self.use_lr_schedules else []},
        ]))
        return optimizer

    @mappable
    def predict(self, doc, force_eval=True, **kwargs):
        if force_eval:
            self.eval()
        with torch.no_grad():
            return self(list(self.preprocessor(doc, only_text=False)), **{"return_loss": False, "return_predictions": True, "group_by_document": True, **kwargs})["predictions"][0]

    save_pretrained = save_pretrained

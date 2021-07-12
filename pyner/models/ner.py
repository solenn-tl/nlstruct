import bisect
import random
import warnings
from math import ceil

import numpy as np
import torch
import transformers

from pyner.data_utils import mappable, huggingface_tokenize, regex_tokenize, slice_document, split_spans, regex_sentencize
from pyner.models.common import Vocabulary, Contextualizer
from pyner.registry import register, get_instance
from pyner.torch_utils import list_factorize, batch_to_tensors, bce_with_logits, nll


def slice_tokenization_output(tokens, begin, end, insert_before=None, insert_after=None):
    index_after_first_token_begin = bisect.bisect_left(tokens["begin"], begin)
    index_before_first_token_end = bisect.bisect_right(tokens["end"], begin)
    index_after_last_token_begin = bisect.bisect_left(tokens["begin"], end)
    index_before_last_token_end = bisect.bisect_right(tokens["end"], end)
    begin_indice = min(index_after_first_token_begin, index_before_first_token_end)
    end_indice = max(index_after_last_token_begin, index_before_last_token_end)
    begins = np.asarray(([begin] if insert_before is not None else []) + list(tokens["begin"][begin_indice:end_indice]) + ([end] if insert_after is not None else []))
    ends = np.asarray(([begin] if insert_before is not None else []) + list(tokens["end"][begin_indice:end_indice]) + ([end] if insert_after is not None else []))

    return {
        "begin": begins,
        "end": ends,
        "text": ([insert_before] if insert_before is not None else []) + list(tokens["text"][begin_indice:end_indice]) + ([insert_after] if insert_after is not None else []),
    }


class LargeSentenceException(Exception):
    pass


@register("ner_preprocessor")
class NERPreprocessor(torch.nn.Module):
    def __init__(
          self,
          bert_name,
          bert_lower=False,
          word_regex='[\\w\']+|[!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~]',
          substitutions=(),
          do_unidecode=True,
          filter_entities=None,
          sentence_split_regex=r"((?:\s*\n)+\s*|(?:(?<=[a-z0-9)]\.)\s+))(?=[A-Z])",
          split_into_multiple_samples=False,
          sentence_balance_chars=(),
          convert_attributes_to_labels=False,
          multi_label=None,
          sentence_entity_overlap="raise",
          max_tokens=512,
          min_tokens=128,
          join_small_sentence_rate=0.5,
          large_sentences="equal-split",
          empty_entities="raise",
          fragment_label_is_entity_label=True,
          keep_bert_special_tokens=False,
          vocabularies={},
    ):
        """
        Preprocess the data
        Since this is a big piece of logic, it was put in a separate class

        :param bert_name:
            Name/path of the transformer model
        :param bert_lower:
            Apply lower case before tokenizing into wordpieces
        :param word_regex: str
            Regex to use to split sentence into words
            Optional: if False, only bert wordpieces will be used
        :param substitutions: list of (str, str)
            (pattern, replacement) regex substitutions to apply on sentence before tokenizing
        :param do_unidecode: bool
            Apply unidecode on strings before tokenizing
        :param sentence_split_regex: str
            Regex used to split sentences.
            Ex: "(\\n([ ]*\\n)*)" will split on newlines / spaces, and not keep these tokens in the sentences, because they are matched in a captured group
        :param sentence_balance_chars: tuple of str
            Characters to "balance" when splitting sentence, ex: parenthesis, brackets, etc.
            Will make sure that we always have (number of '[')  <= (number of ']')
        :param sentence_entity_overlap: str
            What to do when a entity overlaps multiple sentences ?
            Choices: "raise" to raise an error or "split" to split the entity
        :param max_tokens: int
            Maximum number of bert tokens in a sample
        :param large_sentences: str
            One of "equal-split", "max-split", "raise"
            If "equal-split", any sentence longer than max_tokens will be split into
            min number of approx equal size sentences that fit into the model
            If "max-split", make max number of max_tokens sentences, and make a small sentence if any token remains
            If "raise", raises
        :param empty_entities: str
            One of "raise", "drop"
            If "drop", remove any entity that does not contain any word
            If "raise", raises when this happens
        :param vocabularies: dict of (str, Vocabulary)
            Vocabularies that will be used
            To train them (fill them) before training the model, and differ
            the matrices initialization until we know their sizes, make sure
            to call .train() of them before passing them to the __init__
        """
        super().__init__()
        assert empty_entities in ("raise", "drop")
        assert large_sentences in ("equal-split", "max-split", "raise")
        self.tokenizer = transformers.AutoTokenizer.from_pretrained(bert_name) if bert_name is not None else None
        self.sentence_split_regex = sentence_split_regex
        self.split_into_multiple_samples = split_into_multiple_samples
        self.sentence_balance_chars = sentence_balance_chars
        self.sentence_entity_overlap = sentence_entity_overlap
        self.filter_entities = filter_entities
        self.large_sentences = large_sentences
        self.do_unidecode = do_unidecode
        self.bert_lower = bert_lower
        self.word_regex = word_regex
        self.vocabularies = torch.nn.ModuleDict({key: Vocabulary(**vocabulary) for key, vocabulary in vocabularies.items()})
        self.substitutions = substitutions
        self.empty_entities = empty_entities
        if min_tokens is not None and max_tokens is not None:
            assert min_tokens <= max_tokens // 2, "Minimum number of tokens must be at least twice as small as the maximum number of tokens"
        self.max_tokens = max_tokens
        self.min_tokens = min_tokens
        self.join_small_sentence_rate = join_small_sentence_rate
        self.convert_attributes_to_labels = convert_attributes_to_labels
        self.keep_bert_special_tokens = keep_bert_special_tokens
        if convert_attributes_to_labels is True:
            if multi_label is False:
                raise Exception("convert_attributes_to_labels requires multi_label = True")
            else:
                multi_label = True
        self.multi_label = multi_label
        self.fragment_label_is_entity_label = fragment_label_is_entity_label
        if fragment_label_is_entity_label:
            self.vocabularies["fragment_label"] = self.vocabularies["entity_label"]
        self.bert_tokenizer_cache = {}
        self.regex_tokenizer_cache = {}

    @mappable
    def forward(self, doc, only_text=False):
        results = []
        for sample, tokenized_sample, tokenized_sentences in self.sentencize_and_tokenize(doc, only_text=only_text):
            # Here, we know that the sentence is not too long
            if "char" in self.vocabularies:
                words_chars = [[self.vocabularies["char"].get(char) for char in word]
                               for word, word_bert_begin in zip(tokenized_sample["words_text"],
                                                                tokenized_sample["words_bert_begin"]) if word_bert_begin != -1]
            else:
                words_chars = None
            fragments_begin = []
            fragments_end = []
            fragments_label = []
            fragments_id = []
            fragments_entities = []
            entities_label = []
            entities_fragments = []

            tags = None
            if not only_text and "entities" in sample:
                fragments_dict = dict()

                entities = [
                    entity for entity in sample["entities"]
                    if not self.filter_entities or len(set(entity["label"]) & set(self.filter_entities)) > 0
                ]

                tags = [
                    [
                        [False] * len(self.vocabularies["fragment_label"].values)
                        for _ in range(len(tokenized_sample["words_begin"]))
                    ] for _ in range(max(1, len(entities)))
                ]  # n_entities * n_token_labels * n_tokens

                new_sample = {**sample, "entities": []}
                for entity in entities:
                    entity_idx = len(entities_label)
                    new_sample["entities"].append(entity)
                    entities_fragments.append([])
                    entity_label = entity["label"]
                    if self.multi_label and not isinstance(entity_label, (tuple, list)):
                        entity_label = [entity_label]
                    elif not self.multi_label and isinstance(entity_label, (tuple, list)):
                        raise Exception("Entity {} has multiple labels but multi_label parameter is False".format(entity["entity_id"]))
                    if self.convert_attributes_to_labels:
                        if isinstance(entity_label, (tuple, list)):
                            entity_label = [entity_label]
                        for attribute in entity["attributes"]:
                            entity_label.append("{}:{}".format(attribute["label"], attribute["value"]))
                    if isinstance(entity_label, (tuple, list)):
                        entities_label.append([label in entity_label for label in self.vocabularies["entity_label"].values])
                    else:
                        entities_label.append(self.vocabularies["entity_label"].get(entity_label))
                    for fragment_idx, fragment in enumerate(entity["fragments"]):
                        fragment_id = entity["entity_id"] + "/" + str(fragment_idx)
                        key = fragment["begin"], fragment["end"], fragment.get("label", entity["label"] if self.fragment_label_is_entity_label and not self.multi_label else "main")
                        if key in fragments_dict:
                            idx, fragment_id = fragments_dict[key]
                            fragments_entities[idx].append(entity_idx)
                            entities_fragments[-1].append(fragment_id)
                            continue
                        fragments_dict[key] = (len(fragments_entities), fragment_id)
                        entities_fragments[-1].append(fragment_id)
                        fragments_entities.append([entity_idx])
                        fragments_begin.append(fragment["begin"])
                        fragments_end.append(fragment["end"])
                        fragments_label.append(fragment["label"] if not self.fragment_label_is_entity_label else entity["label"])
                        # fragments_label.append(fragment.get("label", "main"))
                        fragments_id.append(fragment_id)
                sample = new_sample
                sorter = sorted(range(len(fragments_begin)), key=lambda i: (fragments_label[i], fragments_begin[i], fragments_end[i]))
                fragments_begin = [fragments_begin[i] for i in sorter]
                fragments_end = [fragments_end[i] for i in sorter]
                fragments_label = [fragments_label[i] for i in sorter]
                fragments_id = [fragments_id[i] for i in sorter]
                fragments_entities = [fragments_entities[i] for i in sorter]

                fragments_begin, fragments_end = split_spans(fragments_begin, fragments_end, tokenized_sample["words_begin"], tokenized_sample["words_end"])
                empty_fragment_idx = next((i for i, begin in enumerate(fragments_begin) if begin == -1), None)
                if empty_fragment_idx is not None:
                    if self.empty_entities == "raise":
                        raise Exception(
                            f"Entity {sample['doc_id']}/{fragments_id[empty_fragment_idx]} could not be matched with any word"
                            f" (is it empty or outside the text ?). Use empty_entities='drop' to ignore these cases")
                    else:
                        warnings.warn("Empty fragments (start = end or outside the text) have been skipped")
                        fragments_label = [label for label, begin in zip(fragments_label, fragments_begin) if begin != -1]
                        fragments_id = [entity_id for entity_id, begin in zip(fragments_id, fragments_begin) if begin != -1]
                        fragments_end = np.asarray([end for end, begin in zip(fragments_end, fragments_begin) if begin != -1])
                        fragments_entities = np.asarray([e for e, begin in zip(fragments_entities, fragments_begin) if begin != -1])
                        fragments_begin = np.asarray([begin for begin in fragments_begin if begin != -1])

                entities_fragments = list_factorize(entities_fragments, fragments_id)[0]
                fragments_end -= 1  # end now means the index of the last word
                fragments_label = [self.vocabularies["fragment_label"].get(label) for label in fragments_label]
                fragments_begin, fragments_end = fragments_begin.tolist(), fragments_end.tolist()

                for entity_idx, (entity_fragments, entity) in enumerate(zip(entities_fragments, entities)):
                    for fragment_idx, fragment in zip(entity_fragments, entity["fragments"]):
                        begin = fragments_begin[fragment_idx]
                        end = fragments_end[fragment_idx]
                        fragment_label = fragments_label[fragment_idx]
                        for i in range(begin, end + 1):
                            tags[entity_idx][i][fragment_label] = True

            if len(entities_label) == 0:
                entities_label = [[False] * len(self.vocabularies["entity_label"].values)] if self.multi_label else [0]
                entities_fragments = [[]]
                entities_mask = [False]
            else:
                entities_mask = [True] * len(entities_fragments)
            if len(fragments_label) == 0:
                fragments_begin = [0]
                fragments_end = [0]
                fragments_label = [0]
                fragments_id = [0]
                fragments_entities = [[]]
                fragments_mask = [False]
            else:
                fragments_mask = [True] * len(fragments_label)
            # if len(tokens_indice) > self.max_tokens:
            results.append({
                "tokens": tokenized_sentences["bert_tokens_indice"],
                "tokens_mask": tokenized_sentences["bert_tokens_mask"],
                "sentence_mask": [True] * len(tokenized_sentences["bert_tokens_indice"]),
                "words_mask": [True] * len(tokenized_sample["words_text"]),
                "words_text": tokenized_sample["words_text"],
                "words_chars_mask": [[True] * len(word_chars) for word_chars in words_chars] if words_chars is not None else None,
                "words_bert_begin": tokenized_sample["words_bert_begin"].tolist(),
                "words_bert_end": tokenized_sample["words_bert_end"].tolist(),
                "words_begin": tokenized_sample["words_begin"].tolist(),
                "words_end": tokenized_sample["words_end"].tolist(),
                "words_chars": words_chars,
                "entities_token_tags": tags,
                "entities_label": entities_label,
                "entities_fragments": entities_fragments,
                "fragments_begin": fragments_begin,
                "fragments_end": fragments_end,
                "fragments_label": fragments_label,
                "fragments_entities": fragments_entities,
                "fragments_mask": fragments_mask,
                "entities_mask": entities_mask,
                "doc_id": sample["doc_id"],
                "original_sample": sample,
                "original_doc": doc,
            })
        return results

    def train(self, mode=True):
        self.training = mode

    def empty_cache(self):
        self.bert_tokenizer_cache = {}
        self.regex_tokenizer_cache = {}

    def sentencize_and_tokenize(self, doc, only_text=False):
        text = doc["text"]

        if self.tokenizer is not None:
            if not self.training or text not in self.bert_tokenizer_cache:
                full_doc_bert_tokens = huggingface_tokenize(text.lower() if self.bert_lower else text,
                                                            tokenizer=self.tokenizer,
                                                            subs=self.substitutions,
                                                            do_unidecode=self.do_unidecode,
                                                            return_offsets_mapping=True,
                                                            add_special_tokens=False)
                if self.training:
                    self.bert_tokenizer_cache[text] = full_doc_bert_tokens
            else:
                full_doc_bert_tokens = self.bert_tokenizer_cache[text]
        else:
            full_doc_bert_tokens = None
        if self.word_regex is not None:
            if not self.training or text not in self.regex_tokenizer_cache:
                full_doc_words = regex_tokenize(text,
                                                reg=self.word_regex,
                                                subs=self.substitutions,
                                                do_unidecode=self.do_unidecode,
                                                return_offsets_mapping=True, )
                if self.training:
                    self.regex_tokenizer_cache[text] = full_doc_words
            else:
                full_doc_words = self.regex_tokenizer_cache[text]
        else:
            full_doc_words = full_doc_bert_tokens
        if full_doc_bert_tokens is None:
            full_doc_bert_tokens = full_doc_words

        if self.sentence_split_regex is not None:
            sentences_bounds = list(regex_sentencize(text, reg_split=self.sentence_split_regex, balance_chars=self.sentence_balance_chars))
        else:
            sentences_bounds = [(0, len(text))]
        if self.split_into_multiple_samples:
            results = []
        else:
            results = [(
                doc, {
                    "words_begin": np.asarray([], dtype=int),
                    "words_end": np.asarray([], dtype=int),
                    "words_bert_begin": np.asarray([], dtype=int),
                    "words_bert_end": np.asarray([], dtype=int),
                    "words_text": []
                }, {
                    "bert_tokens_text": [],
                    "bert_tokens_begin": [],
                    "bert_tokens_end": [],
                    "bert_tokens_indice": [],
                    "bert_tokens_mask": [],
                })]
        bert_offset = 0
        begin = None
        while len(sentences_bounds):
            new_begin, end = sentences_bounds.pop(0)
            if begin is None:
                begin = new_begin

            sentence_text = text[begin:end]
            if not sentence_text.strip():
                continue

            bert_tokens = slice_tokenization_output(full_doc_bert_tokens, begin, end,
                                                    getattr(self.tokenizer, '_bos_token', self.tokenizer.special_tokens_map.get('cls_token', None)) if self.tokenizer is not None else None,
                                                    getattr(self.tokenizer, '_eos_token', self.tokenizer.special_tokens_map.get('sep_token', None)) if self.tokenizer is not None else None)

            if (
                  (self.min_tokens is not None and len(bert_tokens["text"]) < self.min_tokens) or
                  (self.max_tokens is not None and len(bert_tokens["text"]) < self.max_tokens and (not self.training or random.random() < self.join_small_sentence_rate))
            ) and len(sentences_bounds):
                if len(bert_tokens["text"]) + len(slice_tokenization_output(full_doc_bert_tokens, *sentences_bounds[0])["text"]) + 2 < self.max_tokens:
                    continue

            words = slice_tokenization_output(full_doc_words, begin, end, '' if self.keep_bert_special_tokens else None, '' if self.keep_bert_special_tokens else None)
            tokens_indice = self.tokenizer.convert_tokens_to_ids(bert_tokens["text"]) if self.tokenizer is not None else None
            words_bert_begin, words_bert_end = split_spans(words["begin"], words["end"], bert_tokens["begin"], bert_tokens["end"])
            words = {
                key: [i for i, j in zip(value, words_bert_begin) if j != -1] if isinstance(value, list) else value[words_bert_begin != -1]
                for key, value in words.items()
            }
            words_bert_end = words_bert_end[words_bert_begin != -1]
            words_bert_begin = words_bert_begin[words_bert_begin != -1]
            # words_bert_begin, words_bert_end = words_bert_begin.tolist(), words_bert_end.tolist()

            # if the sentence has too many tokens, split it
            if len(bert_tokens['text']) > self.max_tokens:
                warnings.warn(f'Sentences > {self.max_tokens} tokens will be split. Consider using a more restrictive regex for sentence splitting if you want to avoid it.')
                if self.large_sentences == "equal-split":
                    stop_bert_token = max(len(bert_tokens['text']) // ceil(len(bert_tokens['text']) / self.max_tokens), self.min_tokens)
                elif self.large_sentences == "max-split":
                    stop_bert_token = self.max_tokens
                else:
                    raise LargeSentenceException(repr(sentence_text))
                last_word = next(i for i in range(len(words_bert_end) - 1) if words_bert_end[i + 1] >= stop_bert_token)
                sentences_bounds[:0] = [(begin, words["end"][last_word]), (words["begin"][last_word + 1], end)]
                begin = None
                continue
                # else:
                #    print(len(bert_tokens["text"]) + len(slice_tokenization_output(full_doc_bert_tokens, *sentences_bounds[0])["text"]))
            if not self.split_into_multiple_samples:
                # words["begin"] += begin
                # words["end"] += begin
                # if bert_tokens is not words:
                #     bert_tokens["begin"] += begin
                #     bert_tokens["end"] += begin
                words_bert_begin += bert_offset
                words_bert_end += bert_offset
            bert_offset += len(bert_tokens["text"])
            if self.split_into_multiple_samples:
                results.append((
                    slice_document(
                        doc,
                        begin,
                        end,
                        entity_overlap=self.sentence_entity_overlap,
                        only_text=only_text,
                        main_fragment_label="main",
                        offset_spans=True,
                    ),
                    {
                        "words_bert_begin": words_bert_begin,
                        "words_bert_end": words_bert_end,
                        "words_text": words["text"],
                        "words_begin": words["begin"] - begin,
                        "words_end": words["end"] - begin,
                    },
                    {
                        "bert_tokens_text": [bert_tokens["text"]],
                        "bert_tokens_begin": [bert_tokens["begin"]],
                        "bert_tokens_end": [bert_tokens["end"]],
                        "bert_tokens_indice": [tokens_indice],
                        "bert_tokens_mask": [[True] * len(tokens_indice)],
                    }
                ))
            else:
                results[0][1]["words_text"] += words["text"]
                # numpy arrays
                results[0][1]["words_begin"] = np.concatenate([results[0][1]["words_begin"], words["begin"]])
                results[0][1]["words_end"] = np.concatenate([results[0][1]["words_end"], words["end"]])
                results[0][1]["words_bert_begin"] = np.concatenate([results[0][1]["words_bert_begin"], words_bert_begin])
                results[0][1]["words_bert_end"] = np.concatenate([results[0][1]["words_bert_end"], words_bert_end])

                results[0][2]["bert_tokens_text"].append(bert_tokens["text"])
                results[0][2]["bert_tokens_begin"].append(bert_tokens["begin"])
                results[0][2]["bert_tokens_end"].append(bert_tokens["end"])
                if tokens_indice is not None:
                    results[0][2]["bert_tokens_indice"].append(tokens_indice)
                results[0][2]["bert_tokens_mask"].append([True] * len(bert_tokens["text"]))
            begin = None
        return results

    def tensorize(self, batch, device=None):
        return batch_to_tensors(
            batch,
            pad={"entities_fragments": -1, "fragments_entities": -1, "entities_label": -100, "tokens": 0},
            dtypes={
                "words_mask": torch.bool,
                "words_chars": torch.long,
                "words_chars_mask": torch.bool,
                "words_bert_begin": torch.long,
                "words_bert_end": torch.long,
                "words_begin": torch.long,
                "words_end": torch.long,
                "fragments_begin": torch.long,
                "fragments_end": torch.long,
                "fragments_label": torch.long,
                "fragments_entities": torch.long,
                "entities_fragments": torch.long,
                "fragments_mask": torch.bool,
            }, device=device)

    def decode(self, predictions, prep, group_by_document=True):
        pad_tensor = None
        docs = []
        last_doc_id = None
        for doc_prep, doc_predictions in zip(prep, predictions):
            doc_id = doc_prep["doc_id"].rsplit("/", 1)[0]
            if group_by_document:
                text = doc_prep["original_doc"]["text"]
                char_offset = doc_prep["original_sample"].get("begin", 0)
                if doc_id != last_doc_id:
                    last_doc_id = doc_id
                    docs.append({
                        "doc_id": doc_id,
                        "text": doc_prep["original_doc"]["text"],
                        "entities": [],
                    })
            else:
                char_offset = 0
                text = doc_prep["original_sample"]["text"]
                docs.append({
                    "doc_id": doc_prep["doc_id"],
                    "text": text,
                    "entities": [],
                })
            for entity in doc_predictions:
                if isinstance(entity["label"], (tuple, list)):
                    label = [self.vocabularies['entity_label'].values[l] for l in entity["label"]]
                else:
                    label = [self.vocabularies['entity_label'].values[entity["label"]]]
                res_entity = {
                    "label": label,
                    "attributes": [
                        {"label": label.split(":")[0], "value": (label.split(":")[1] or None)}
                        for label in label if ":" in label
                    ] if self.convert_attributes_to_labels else [],
                    "fragments": [{
                        "begin": char_offset + doc_prep["words_begin"][f["begin"]],
                        "end": char_offset + doc_prep["words_end"][f["end"]],
                        "label": self.vocabularies['fragment_label'].values[f["label"]],
                        "text": text[char_offset + doc_prep["words_begin"][f["begin"]]:char_offset + doc_prep["words_end"][f["end"]]],
                    } for f in entity["fragments"]],
                    "confidence": entity["confidence"],
                }
                docs[-1]["entities"].append(res_entity)
        return docs


class SpanLoss(torch.nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, scores, mask, target):
        if target.ndim == scores.ndim - 1:
            target = target.unsqueeze(0).repeat_interleave(scores.shape[0], dim=0)
        loss = (bce_with_logits(
            scores,
            target, reduction='none').masked_fill(~mask, 0))
        loss = loss.mean(-1).sum()
        # loss = loss.sum()#(loss / mask.rename(None).sum(-1, keepdim=True).sum(-2, keepdim=True).sum(-3, keepdim=True)).sum() * 10
        return loss


class MarginalTagLoss(torch.nn.Module):
    def __init__(self, positive_only=False):
        super().__init__()
        self.positive_only = positive_only

    def forward(self, tag_logprobs, label_logits, batch):
        if tag_logprobs is None:
            return 0
        if tag_logprobs.ndim == 5:
            shape = tag_logprobs.shape[1:-1]
        else:
            shape = tag_logprobs.shape[:-1]
        O, I, B, L, U = 0, 1, 2, 3, 4
        begins = batch["fragments_begin"]  # n_samples * n_fragments
        ends = batch["fragments_end"]  # n_samples * n_fragments
        labels = batch["fragments_label"]  # n_samples * n_fragments
        fragment_mask = batch["fragments_mask"]  # n_samples * n_fragments
        i_tags = torch.zeros(shape, dtype=torch.long)
        bl_tags = torch.zeros(shape, dtype=torch.long)
        u_tags = torch.zeros(shape, dtype=torch.long)
        mask = batch['words_mask']
        for sample_idx, b, e, l in zip(fragment_mask.nonzero(as_tuple=True)[0].tolist(), begins[fragment_mask].tolist(), ends[fragment_mask].tolist(), labels[fragment_mask].tolist()):
            if b < e:
                i_tags[sample_idx, l, b:e + 1] = I
                bl_tags[sample_idx, l, b] = B
                bl_tags[sample_idx, l, e] = L
            else:
                u_tags[sample_idx, l, b] = U
        tags = torch.maximum(torch.maximum(i_tags, bl_tags), u_tags).to(tag_logprobs.device)
        if self.positive_only:
            mask = mask & (tags > 0).any(1)  # & shift(tags_target.any(-1), dim=1, n=1) & shift(tags_target.any(-1), dim=1, n=-1)
        loss = nll(
            tag_logprobs,
            tags if tag_logprobs.ndim == 4 else tags.unsqueeze(0).repeat_interleave(tag_logprobs.shape[0], dim=0),
            reduction='none',
        ).masked_fill(~mask.unsqueeze(1), 0).mean(-2).sum()

        loss = loss + bce_with_logits(
            label_logits,
            tags if label_logits.ndim == 3 else tags.unsqueeze(0).repeat_interleave(label_logits.shape[0], dim=0) > 0,
            reduction='none',
        ).masked_fill(~mask.unsqueeze(1), 0).mean(-2).sum()
        # loss = loss.mean(-1).sum()
        return loss


@register("span_scorer")
class SpanScorer(torch.nn.Module):
    def forward(self, features, mask, batch, force_gold=False):
        raise NotImplementedError()

    def loss(self, spans, batch):
        raise NotImplementedError()


@register("contiguous_entity_decoder")
class ContiguousEntityDecoder(torch.nn.Module):
    def __init__(self,
                 contextualizer=None,
                 span_scorer=dict(),
                 intermediate_loss_slice=slice(None),
                 _classifier=None,
                 _preprocessor=None,
                 _encoder=None,
                 ):
        super().__init__()

        input_size = _encoder.output_size
        labels = _preprocessor.vocabularies["entity_label"].values
        # Pre decoder module
        if contextualizer is not None:
            self.contextualizer = Contextualizer(**{**contextualizer, "input_size": input_size})
        else:
            self.contextualizer = None

        self.n_labels = n_labels = len(labels)
        self.span_scorer = SpanScorer(**{
            "input_size": input_size if contextualizer is None else self.contextualizer.output_size,
            "n_labels": n_labels,
            **span_scorer,
        })
        self.intermediate_loss_slice = intermediate_loss_slice

    def on_training_step(self, step_idx, total):
        pass

    def fast_params(self):
        return self.span_scorer.fast_params()

    def forward(self, words_embed, batch=None, return_loss=False, return_predictions=False):
        ############################
        # Generate span candidates #
        ############################
        if isinstance(words_embed, tuple):
            words_embed, lm_embeds = words_embed
        words_mask = batch['words_mask']

        if self.contextualizer is not None:
            contextualized_words_embed = self.contextualizer(words_embed, words_mask, return_all_layers=True)
        else:
            contextualized_words_embed = words_embed.unsqueeze(0)

        spans = self.span_scorer(contextualized_words_embed[self.intermediate_loss_slice if return_loss else slice(-1, None)],
                                 words_mask, batch, force_gold=return_loss)

        #########################
        # Compute the span loss #
        #########################
        loss_dict = {}
        if return_loss:
            loss_dict = self.span_scorer.loss(spans, batch)

        predictions = None
        if return_predictions:
            spans_mask = spans["flat_spans_mask"]
            predictions = [[] for _ in batch["original_sample"]]
            if 0 not in spans_mask.shape:
                # entities_confidence = entities_label_scores.detach().cpu()[-1].sigmoid().masked_fill(~entities_label, 1).prod(-1)
                # for sample_idx, entity_idx in (~entities_label[..., 0]).masked_fill(~entities_mask.cpu(), False).nonzero(as_tuple=False).tolist():
                for sample_idx, fragment_idx in spans_mask.nonzero(as_tuple=False).tolist():
                    predictions[sample_idx].append({
                        "entity_id": len(predictions[sample_idx]),
                        "confidence": 1.,  # entities_confidence[sample_idx, entity_idx].item(),
                        "label": spans["flat_spans_label"][sample_idx, fragment_idx].item(),
                        "fragments": [
                            {
                                "begin": spans["flat_spans_begin"][sample_idx, fragment_idx].item(),
                                "end": spans["flat_spans_end"][sample_idx, fragment_idx].item(),
                                "label": spans["flat_spans_label"][sample_idx, fragment_idx].item(),
                            }
                        ]
                    })

        return {
            "predictions": predictions,
            **loss_dict,
            **spans,
        }

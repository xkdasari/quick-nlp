import json
import os
import pickle
from glob import glob
from operator import itemgetter
from pathlib import Path
from typing import Iterator, List, Optional, Tuple, Union

import numpy as np
import pandas as pd
import torch
from fastai.core import A
from torchtext.data import Dataset, Example, Field
from tqdm import tqdm

NamedField = Tuple[str, Field]


class TabularDatasetFromFiles(Dataset):
    """This class allows the loading of multiple column data from a tabular format (e.g. csv, tsv, json, Similar to torchtext
    TabularDataset class. The difference is it can work through a directory of multiple files instead of only
    a single file
    """

    def get_examples_from_file(self, path: str, fields: List[NamedField], format: str, encoding: str = 'utf-8',
                               skip_header: bool = True) -> Tuple[List[Example], List[NamedField]]:
        if format.lower() in ["csv", "tsv"]:
            sep = "," if format.lower() == "csv" else "\t"
            data = pd.read_csv(os.path.expanduser(path), encoding=encoding, header=0 if skip_header else None,
                               sep=sep)
        elif format.lower() == "json":
            data = pd.read_json(os.path.expanduser(path), encoding=encoding)
        examples = []
        for _, row in data.iterrows():
            examples.append(Example.fromlist(row.values.tolist(), fields))
        return examples, fields

    def __init__(self, path: str, fields: List[NamedField], encoding: str = 'utf-8', skip_header: bool = False,
                 **kwargs):
        paths = glob(f'{path}/*.*') if os.path.isdir(path) else [path]
        examples = []
        for path_ in paths:
            examples_from_file, fields = self.get_examples_from_file(path_, fields,
                                                                     format=os.path.splitext(path_)[-1][1:],
                                                                     skip_header=skip_header,
                                                                     encoding=encoding)
            examples.extend(examples_from_file)

        super().__init__(examples, fields, **kwargs)


class TabularDatasetFromDataFrame(Dataset):

    @classmethod
    def columns(cls, fields: List[NamedField]) -> List[str]:
        return [i[0] for i in fields]

    def __init__(self, df, fields, **kwargs):
        df = df.loc[:, self.columns(fields)]
        examples = []
        for index, row in df.iterrows():
            example = Example.fromlist(row.tolist(), fields)
            examples.append(example)

        super().__init__(examples, fields, **kwargs)

    @classmethod
    def splits(cls, train_df: Optional[pd.DataFrame] = None, val_df: Optional[pd.DataFrame] = None,
               test_df: Optional[pd.DataFrame] = None, **kwargs) -> Tuple['TabularDatasetFromDataFrame', ...]:
        train_data = None if train_df is None else cls(train_df, **kwargs)
        val_data = None if val_df is None else cls(val_df, **kwargs)
        test_data = None if test_df is None else cls(test_df, **kwargs)

        return tuple(d for d in (train_data, val_data, test_data) if d is not None)


def df_to_dialogue_examples(df: pd.DataFrame, *, fields: List[Tuple[str, Field]], batch_col: str,
                            role_col: str, text_col: str, sort_col: str, max_sl=1000) -> Iterator[Example]:
    """convert df to dialogue examples"""
    df = [df] if not isinstance(df, list) else df
    tokenize = fields[0][1].tokenize
    for file_index, _df in enumerate(df):
        for chat_id, conversation in tqdm(_df.groupby(batch_col), desc=f"processed file {file_index}/{len(df)}"):
            if conversation[role_col].nunique() > 1:
                conversation = conversation.sort_values(by=sort_col)
                conversation_tokens = "__" + conversation[role_col] + "__"
                text_with_roles = (conversation_tokens + " " + conversation[text_col]).astype(str)
                text_with_roles_length = text_with_roles.apply(lambda x: len(tokenize(x)))
                text = "".join(text_with_roles.str.cat(sep=" "))
                roles = "".join(conversation_tokens.str.cat(sep=" "))
                example = Example.fromlist([text.strip(), roles.strip()], fields)
                example.sl = text_with_roles_length.tolist()
                # sanity check if the sl is much larger than expected ignore
                if max(example.sl) < max_sl:
                    yield example


def json_to_dialogue_examples(path_dir: Path, *, fields: List[Tuple[str, Field]], utterance_key: str, role_key: str,
                              text_key: str, sort_key: str, max_sl: int = 1000,
                              target_roles: Optional[List[str]] = None) -> \
        Iterator[Example]:
    """Load dialogues from json files
    a json file should have a List of Dicts, see examples:
     [{batch_col:chat_id, utterance_col:[{text_col:message, role_col:role, sort_col:timestamp}]}]

    """
    for file_index, file in enumerate(path_dir.glob("*.json")):
        with file.open('r', encoding='utf-8') as fh:
            dialogues = json.load(fh)
        for dialogue in tqdm(dialogues, desc=f'processed file {file}'):
            if isinstance(sort_key, str):
                key = itemgetter(sort_key)
            elif callable(sort_key):
                key = sort_key
            else:
                raise ValueError("Invalid sort_key provided")
            conversation = sorted(dialogue[utterance_key], key=key)
            text = ""
            roles = ""
            lengths = []
            tokenize = fields[0][1].tokenize
            for utterance in conversation:
                ut = utterance[text_key]
                ut = " ".join(ut) if isinstance(ut, list) else ut
                conv_role = "__" + utterance[role_key] + "__"
                text_with_role = conv_role + " " + ut
                if text.strip() != "":
                    if target_roles is None or utterance[role_key] in target_roles:
                        example = Example.fromlist([text.strip(), roles.strip(), text_with_role], fields)
                        example.sl = [i for i in lengths]
                        # sanity check if the sl is much larger than expected ignore
                        assert len(lengths) == len(roles.split())
                        if max(example.sl) < max_sl:
                            yield example
                text += " " + text_with_role
                roles += " " + conv_role
                lengths.append(len(tokenize(text_with_role)))


class HierarchicalDatasetFromDataFrame(Dataset):

    def __init__(self, df: Union[pd.DataFrame, List[pd.DataFrame]], text_field: Field, batch_col: str,
                 text_col: str, role_col: str, sort_col: str, path: Optional[str] = None, max_sl: int = 1000,
                 reset: bool = False, **kwargs):
        """

        Args:
            df (Union[pd.DataFrame, List[pd.DataFrame]]: A dataframe or a list of dataframes with the data
            text_field (Field): a torchtext.data.Field object that will process the tokenizations
            batch_col (str): The name of the column in the data df that will be used to group the conversations, e.g. chat_id
            text_col (str): The name of the column in the data containing the text data, e.g. body
            role_col (str): The name of the column in the data containing the role/name of the person speaking, e.g. role
            sort_col (str): The name of the column in the data that will be used to sort the data of every group, e.g. timestamp
            reset (bool): If true and example pickles exist delete them
            **kwargs:
        """
        fields = [("text", text_field), ("roles", text_field)]
        iterator = df_to_dialogue_examples(df, fields=fields, batch_col=batch_col, role_col=role_col,
                                           sort_col=sort_col, text_col=text_col, max_sl=max_sl)
        if path is not None:
            path = Path(path)
            examples_pickle = path / "examples.pickle"
            if examples_pickle.exists() and not reset:
                with examples_pickle.open("rb") as fh:
                    examples = pickle.load(fh)
            else:
                with examples_pickle.open('wb') as fh:
                    examples = [i for i in iterator]
                    pickle.dump(examples, fh)
        else:
            examples = [i for i in iterator]
        super().__init__(examples=examples, fields=fields, **kwargs)

    @classmethod
    def splits(cls, path: Optional[str] = None, train_df: Optional[pd.DataFrame] = None,
               val_df: Optional[pd.DataFrame] = None, test_df: Optional[pd.DataFrame] = None,
               max_sl: int = 1000, **kwargs) -> Tuple['HierarchicalDatasetFromDataFrame', ...]:
        train_data = None if train_df is None else cls(path=path, df=train_df, max_sl=max_sl, **kwargs)
        val_data = None if val_df is None else cls(path=path, df=val_df, **kwargs)
        test_data = None if test_df is None else cls(path=path, df=test_df, **kwargs)

        return tuple(d for d in (train_data, val_data, test_data) if d is not None)


def load_dfs(paths: str, file_format: str, encoding: Optional[str] = None) -> List[pd.DataFrame]:
    if file_format in ["csv", "tsv"]:
        sep = {"csv": ",", "tsv": "\t"}[file_format]
        return [pd.read_csv(path, sep=sep, encoding=encoding) for path in paths if path.endswith(file_format)]
    elif file_format == "json":
        return [pd.read_json(path, encoding=encoding) for path in paths if path.endswith(file_format)]


class HierarchicalDatasetFromFiles(HierarchicalDatasetFromDataFrame):
    def __init__(self, path, file_format, text_field: Field, batch_col: str, text_col: str, role_col: str,
                 sort_col: Optional[str] = None, encoding: Optional[str] = None, max_sl: int = 1000, **kwargs):
        paths = glob(f'{path}/*.*') if os.path.isdir(path) else [path]
        dfs = load_dfs(paths, file_format=file_format, encoding=encoding)
        super().__init__(path=path, df=dfs, text_field=text_field, batch_col=batch_col, text_col=text_col,
                         role_col=role_col, sort_col=sort_col, max_sl=max_sl, **kwargs)

    @classmethod
    def splits(cls, path: str, train_path: Optional[str] = None, val_path: Optional[str] = None,
               test_path: Optional[str] = None, max_sl: int = 1000, **kwargs) -> Tuple[
        'HierarchicalDatasetFromFiles', ...]:
        train_data = None if train_path is None else cls(path=os.path.join(path, train_path), max_sl=max_sl, **kwargs)
        val_data = None if val_path is None else cls(path=os.path.join(path, val_path), max_sl=max_sl, **kwargs)
        test_data = None if test_path is None else cls(path=os.path.join(path, test_path), max_sl=max_sl, **kwargs)

        return tuple(d for d in (train_data, val_data, test_data) if d is not None)


class DialogueDataset(Dataset):

    def __init__(self, path: Union[Path, str], text_field: Field, utterance_key: str,
                 text_key: str, role_key: str, sort_key: str, max_sl: int = 1000, reset=False, target_roles=None,
                 **kwargs):
        """

        Args:
            path (Path,str): the path to a directory with json files to load
            text_field (Field): a torchtext Field object that will tokenize the data
            utterance_key (str): The name of the key in the data that will be contain the utterances (e.g. utterances)
            text_key (str): The name of the key in the json containing the text data
            role_key (str): The name of the key in the json containing the role/name of the person speaking
            sort_key (str): The name of the key in the json that will be used to sort the data of every group
            reset (bool): If true and example pickles exist delete them
            target_roles (Optional[List[str]]): Optionally the roles that will be targets

            **kwargs:

            An example json could be like this:

            [   {utterance_col:[
                    {text_col:body, sort_col:time1, role_col:user1},
                    {text_col:body, sort_col:time2, role_col:user2}]
                 },
                {utterance_col:[
                    {text_col:body, sort_col:time1, role_col:user1},
                    {text_col:body, sort_col:time2, role_col:user2}]
                 }
            ]
        """
        path = Path(path) if isinstance(path, str) else path
        fields = [("text", text_field), ("roles", text_field), ("response", text_field)]
        iterator = json_to_dialogue_examples(path_dir=path, fields=fields, utterance_key=utterance_key,
                                             role_key=role_key, text_key=text_key, sort_key=sort_key, max_sl=max_sl,
                                             target_roles=target_roles
                                             )
        if path is not None:
            examples_pickle = path / "examples.pickle"
            if examples_pickle.exists() and not reset:
                with examples_pickle.open("rb") as fh:
                    examples = pickle.load(fh)
            else:
                with examples_pickle.open('wb') as fh:
                    examples = [i for i in iterator]
                    pickle.dump(examples, fh)
        else:
            examples = [i for i in iterator]
        super().__init__(examples=examples, fields=fields, **kwargs)

    @classmethod
    def splits(cls, path: str, train_path: Optional[str] = None, val_path: Optional[str] = None,
               test_path: Optional[str] = None, max_sl: int = 1000, **kwargs) -> Tuple[
        'DialogueDataset', ...]:
        path = Path(path)
        train_data = None if train_path is None else cls(path=path / train_path, max_sl=max_sl, **kwargs)
        val_data = None if val_path is None else cls(path=path / val_path, max_sl=max_sl, **kwargs)
        test_data = None if test_path is None else cls(path=path / test_path, max_sl=max_sl, **kwargs)

        return tuple(d for d in (train_data, val_data, test_data) if d is not None)


class ContextResponseDataset(Dataset):
    def __init__(self, context: List[int], response: List[int], label: Optional[int] = None, backwards=False,
                 sos: Optional[int] = None,
                 eos: Optional[int] = None):
        self.c, self.r, self.l, self.backwards, self.sos, self.eos = context, response, label, backwards, sos, eos

    def __getitem__(self, idx):
        x = self.c[idx]
        y = self.r[idx]
        label = None if self.label is None else self.l[idx]
        if self.backwards: x = list(reversed(x))
        if self.eos is not None:
            x = x + [self.eos]
            y = y + [self.eos]
        if self.sos is not None:
            x = [self.sos] + x
            y = [self.sos] + y
        if label is None:
            return np.array(x), np.array(y)
        else:
            return np.array(x), np.array(y), label

    def __len__(self):
        return len(self.x)


class DialDataset(Dataset):
    def __init__(self, context: List[List[int]], response: List[int], pad: int, label: Optional[int] = None,
                 backwards=False,
                 sos: Optional[int] = None,
                 eos: Optional[int] = None,
                 ):
        self.c, self.r, self.l, self.backwards, self.sos, self.eos, self.pad = context, response, label, backwards, sos, eos, pad

    def __getitem__(self, idx):
        x = self.c[idx]
        y = self.r[idx]
        if self.backwards: x = [list(reversed(i)) for i in x]
        if self.eos is not None:
            x = [i + [self.eos] for i in x]
            y = y + [self.eos]
        if self.sos is not None:
            x = [[self.sos] + i for i in x]
            y = [self.sos] + y

        max_sl = max([len(i) for i in x])
        x_padded = np.zeros((len(x), max_sl), dtype=np.int64)
        for i, row in enumerate(x):
            x_padded[i, :len(row)] = row
        return x_padded, np.array(y)

    def __len__(self):
        return len(self.c)


class HREDDataset(torch.utils.data.Dataset):
    def __init__(self, x, y):
        self.x, self.y = x, y

    def __getitem__(self, idx):
        return A(np.atleast_2d(self.x[idx]), self.y[idx],
                 np.hstack((self.y[idx][1:], np.asarray([2]))))

    def __len__(self):
        return len(self.x)


class HREDConstraintsDataset(torch.utils.data.Dataset):
    def __init__(self, x, c, y):
        self.x, self.c, self.y = x, c, y

    def __getitem__(self, idx):
        return A(np.atleast_2d(self.x[idx]), self.c[idx], self.y[idx],
                 np.hstack((self.y[idx][1:], np.asarray([2]))))

    def __len__(self):
        return len(self.x)

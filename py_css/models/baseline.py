import models.base as base_module
import models.T5Rewriter as t5_rewriter

from typing import Tuple

import pandas as pd
import pyterrier as pt
from pyterrier_t5 import MonoT5ReRanker, DuoT5ReRanker

import torch

BATCH_SIZE = 64 if torch.cuda.is_available() else 8


class Baseline(base_module.Pipeline):
    """
    A class to represent the baseline retrieval method.
    """

    t5_qr: t5_rewriter.T5Rewriter
    top_docs: Tuple[pt.Transformer, int]
    mono_t5: Tuple[MonoT5ReRanker, int]
    duo_t5: Tuple[DuoT5ReRanker, int]

    def __init__(
        self,
        index,
        *,
        bm25_docs: int,
        mono_t5_docs: int,
        duo_t5_docs: int,
    ):
        """
        Constructs all the necessary attributes for the baseline retrieval method.

        Parameters
        ----------
        index : pt.Index
            The PyTerrier index.
        bm25_docs : int
            The number of documents to retrieve with BM25.
        mono_t5_docs : int
            The number of documents to retrieve with MonoT5.
        duo_t5_docs : int
            The number of documents to retrieve with DuoT5.
        """
        self.t5_qr = t5_rewriter.T5Rewriter()
        bm25 = pt.BatchRetrieve(index, wmodel="BM25", metadata=["docno", "text"])
        self.top_docs = ((bm25 % bm25_docs).compile(), bm25_docs)
        self.mono_t5 = (MonoT5ReRanker(batch_size=BATCH_SIZE), mono_t5_docs)
        # self.mono_t5 = (MonoT5ReRanker(batch_size=BATCH_SIZE, model="castorini/monot5-large-msmarco"), mono_t5_docs)
        self.duo_t5 = (DuoT5ReRanker(batch_size=BATCH_SIZE), duo_t5_docs)

    def transform_input(
        self, query: base_module.Query, context: base_module.Context
    ) -> str:
        history = []
        for q, _ in context:
            history.append(q.query)
        doc_was_added = False
        if len(context) > 0:
            last_docs = context[-1][1]
            if (
                last_docs is not None
                and len(last_docs) > 0
                and last_docs[0].docno != base_module.EMPTY_PLACEHOLDER_DOC.docno
            ):
                history.append(last_docs[0].content)
                doc_was_added = True
        sum_of_lengths = sum([len(q) for q in history]) + len(query.query)
        if sum_of_lengths > 512:
            if doc_was_added:
                if sum_of_lengths - 512 < len(history[-1]):
                    history[-1] = history[-1][: sum_of_lengths - 512]
                elif sum_of_lengths - 512 == len(query.query):
                    history = history[:-1]
                else:
                    history = history[:-1]
                    remaining = sum([len(q) for q in history]) + len(query.query) - 512
                    while remaining > 0 and 0 < len(history):
                        if len(history[0]) < remaining:
                            remaining -= len(history[0])
                            history = history[0:]
                        else:
                            history[0] = history[0][remaining:]
                            remaining = 0
            else:
                remaining = sum([len(q) for q in history]) + len(query.query) - 512
                while remaining > 0 and 0 < len(history):
                    if len(history[0]) < remaining:
                        remaining -= len(history[0])
                        history = history[0:]
                    else:
                        history[0] = history[0][remaining:]
                        remaining = 0

        history.append(query.query)
        new_query = t5_rewriter.SEPERATOR_TOKEN.join(history)
        return new_query

    def transform(self, query_df: pd.DataFrame) -> pd.DataFrame:
        unique_qids = set(query_df["qid"].unique())

        rewritten_queries_df = self.t5_qr.transform(query_df)

        top_docs_df = self.top_docs[0].transform(rewritten_queries_df.copy())
        top_docs_df = (
            top_docs_df.sort_values(["qid", "score"], ascending=False)
            .groupby("qid")
            .head(self.top_docs[1])
        )

        mono_t5_df = self.mono_t5[0].transform(
            top_docs_df.groupby("qid").head(self.mono_t5[1])
        )
        mono_t5_df = (
            mono_t5_df.sort_values(["qid", "score"], ascending=False)
            .groupby("qid")
            .head(self.mono_t5[1])
        )

        duo_t5_df = self.duo_t5[0].transform(
            mono_t5_df.groupby("qid").head(self.duo_t5[1])
        )
        duo_t5_df = (
            duo_t5_df.sort_values(["qid", "score"], ascending=False)
            .groupby("qid")
            .head(self.duo_t5[1])
        )

        result = self.combine_result_stages([top_docs_df, mono_t5_df, duo_t5_df])
        result = self.pad_empty_documents(
            result, unique_qids, self.top_docs[1], rewritten_queries_df
        )

        return result

from abc import ABC, abstractmethod
from ...common_imports import *
import pyarrow as pa
import pyarrow.parquet as pq


class RelStorage(ABC):
    """
    Responsible for the low-level (i.e., unaware of mandala-specific concepts)
    interactions with the relational part of the storage, such as creating and
    extending tables, running queries, etc. This is intended to be a pretty
    generic, minimal database interface, supporting just the things we need.

    It's deliberately referred to as "relational storage" as opposed to a
    "relational database" because simpler implementations exist.
    """

    @abstractmethod
    def create_relation(self, name: str, columns: List[tuple[str, str]]):
        """
        Create a relation with the given name and columns.
        """
        raise NotImplementedError()

    @abstractmethod
    def delete_relation(self, name: str):
        raise NotImplementedError()

    @abstractmethod
    def create_column(self, relation: str, name: str, default_value: str):
        raise NotImplementedError()

    @abstractmethod
    def insert(self, name: str, df: pd.DataFrame):
        """
        Append rows to a table
        """
        raise NotImplementedError()

    @abstractmethod
    def upsert(self, name: str, df: pa.Table):
        """
        Upsert rows in a table based on index
        """
        raise NotImplementedError()

    @abstractmethod
    def delete(self, name: str, index: List[str]):
        """
        Delete rows from a table based on index
        """
        raise NotImplementedError()

    @abstractmethod
    def get_data(self, table: str) -> pd.DataFrame:
        """
        Fetch data from a table.
        """
        raise NotImplementedError()

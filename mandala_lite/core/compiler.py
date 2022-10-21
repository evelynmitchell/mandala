import pickle
from ..common_imports import *
from .config import Config, dump_output_name
from .weaver import ValQuery, FuncQuery
from .utils import invert_dict
from pypika import Query, Table, Field, Column, Criterion


def concat_lists(lists: List[list]) -> list:
    return [x for lst in lists for x in lst]


def traverse_all(val_queries: List[ValQuery]) -> Tuple[List[ValQuery], List[FuncQuery]]:
    """
    Extend the given `ValQuery` objects to all objects connected to them through
    function inputs/outputs.
    """
    val_queries_ = [_ for _ in val_queries]
    op_queries_: List[FuncQuery] = []
    found_new = True
    while found_new:
        found_new = False
        val_neighbors = concat_lists([v.neighbors() for v in val_queries_])
        op_neighbors = concat_lists([o.neighbors() for o in op_queries_])
        if any(k not in op_queries_ for k in val_neighbors):
            found_new = True
            for neigh in val_neighbors:
                if neigh not in op_queries_:
                    op_queries_.append(neigh)
        if any(k not in val_queries_ for k in op_neighbors):
            found_new = True
            for neigh in op_neighbors:
                if neigh not in val_queries_:
                    val_queries_.append(neigh)
    return val_queries_, op_queries_


class Compiler:
    def __init__(self, val_queries: List[ValQuery], func_queries: List[FuncQuery]):
        self.val_queries = val_queries
        self.func_queries = func_queries
        # self._generate_aliases()
        self.val_aliases, self.func_aliases = self._generate_aliases()

    def _generate_aliases(self) -> Tuple[Dict[ValQuery, Table], Dict[FuncQuery, Table]]:
        func_aliases = {}
        for func_query in self.func_queries:
            op_table = Table(func_query.func_op.sig.versioned_ui_name)
            func_aliases[func_query] = op_table.as_(f"_{id(func_query)}")
        val_aliases = {}
        for val_query in self.val_queries:
            val_table = Table(Config.vref_table)
            val_aliases[val_query] = val_table.as_(f"_{id(val_query)}")
        return val_aliases, func_aliases

    def compile_func(self, op_query: FuncQuery) -> Tuple[list, list]:
        """
        Compile the query corresponding to an op, including built-in ops
        """
        constraints = []
        select_fields = []
        func_alias = self.func_aliases[op_query]
        for input_name, val_query in op_query.inputs.items():
            val_alias = self.val_aliases[val_query]
            constraints.append(val_alias[Config.uid_col] == func_alias[input_name])
            select_fields.append(val_alias[Config.uid_col])
        for output_idx, val_query in enumerate(op_query.outputs):
            val_alias = self.val_aliases[val_query]
            constraints.append(
                val_alias[Config.uid_col]
                == func_alias[dump_output_name(index=output_idx)]
            )
            select_fields.append(val_alias[Config.uid_col])
        return constraints, select_fields

    def compile(self, select_queries: List[ValQuery]):
        """
        Compile the query induced by the data of this compiler instance to
        an SQL select query.

        NOTE:
            - for each value query, we select both columns of the variable
            table: the index and the partition. This is to be able to convert
            the query result directly into locations.
            - The list of columns, partitioned into sublists per value query, is
            also returned.
        """
        # if select_queries is None:
        #     select_queries = tuple(self.val_queries)
        # assert all([vq in self.val_queries for vq in select_queries])
        from_tables = []
        all_constraints = []
        select_cols = [self.val_aliases[vq][Config.uid_col] for vq in select_queries]
        for func_query in self.func_queries:
            constraints, select_fields = self.compile_func(func_query)
            func_alias = self.func_aliases[func_query]
            from_tables.append(func_alias)
            all_constraints += constraints
        for val_query in self.val_queries:
            val_alias = self.val_aliases[val_query]
            from_tables.append(val_alias)
        query = Query
        for table in from_tables:
            query = query.from_(table)
        query = query.select(*select_cols)
        query = query.where(Criterion.all(all_constraints))
        return query


class QueryGraph:
    """
    Represents the graph expressing a query in a form that is suitable for
    incrementally computing the join of all the tables.

    Used as an alternative to a RDBMS engine for computing queries.
    """

    def __init__(
        self,
        val_queries: List[ValQuery],
        func_queries: List[FuncQuery],
        tables: Dict[FuncQuery, pd.DataFrame],
    ):
        # list of participating ValQueries
        self.val_queries = val_queries
        # list of participating FuncQueries
        self.func_queries = func_queries
        # {func query: table of data}. Note that there may be multiple func
        # queries with the same table, but we keep separate references to each
        # in order to enable recursively joining nodes in the graph.
        self.tables = tables
        for k, v in self.tables.items():
            if Config.uid_col in v.columns:
                v.drop(columns=[Config.uid_col], inplace=True)

    @staticmethod
    def from_mandala(
        val_queries: List[ValQuery],
        func_queries: List[FuncQuery],
        call_data: Dict[str, pd.DataFrame],
    ) -> "QueryGraph":
        tables = {
            f: call_data[f.func_op.sig.versioned_internal_name] for f in func_queries
        }
        return QueryGraph(val_queries, func_queries, tables)

    def _get_col_to_vq_mappings(
        self, func_query: FuncQuery
    ) -> Tuple[Dict[str, ValQuery], Dict[ValQuery, List[str]]]:
        """
        Given a FuncQuery, returns:
            - a mapping from column names to the ValQuery that they point to
            - a mapping from ValQuery objects to the list of column names that point to it
        """
        df = self.tables[func_query]
        col_to_vq = {}
        vq_to_cols = defaultdict(list)
        for name, val_query in func_query.inputs.items():
            assert name in df.columns
            col_to_vq[name] = val_query
            vq_to_cols[val_query].append(name)
        for i, val_query in enumerate(func_query.outputs):
            output_name = dump_output_name(index=i)
            assert output_name in df.columns
            col_to_vq[output_name] = val_query
            vq_to_cols[val_query].append(output_name)
        return col_to_vq, vq_to_cols

    def _drop_self_constraints(
        self, df: pd.DataFrame, vq_to_cols: Dict[ValQuery, List[str]]
    ) -> Tuple[pd.DataFrame, Dict[str, ValQuery], Dict[ValQuery, str]]:
        new_col_to_vq = {}
        df = df.copy()
        for vq, cols in vq_to_cols.items():
            if len(cols) > 1:
                representative = cols[0]
                for col in cols[1:]:
                    df = df[df[representative] == df[col]]
                    df.drop(columns=col, inplace=True)
            new_col_to_vq[cols[0]] = vq
        new_vq_to_col = {vq: col for col, vq in new_col_to_vq.items()}
        return df, new_col_to_vq, new_vq_to_col

    def _join_dataframes(
        self,
        df1: pd.DataFrame,
        df2: pd.DataFrame,
        left_on: List[str],
        right_on: List[str],
    ) -> Tuple[pd.DataFrame, Dict[str, str], Dict[str, str]]:
        """
        Join two dataframes along the specified dimensions.

        Returns
            - the result,
            - together with functions from the columns of each dataframe to the
            columns of the result.
        """
        assert len(left_on) == len(right_on)
        # rename the dataframe columns to avoid conflicts
        mapping1 = {col: f"input_{i}" for i, col in enumerate(df1.columns)}
        ncols1 = len(df1.columns)
        mapping2 = {col: f"input_{i + ncols1}" for i, col in enumerate(df2.columns)}
        renamed_df1 = df1.rename(columns=mapping1)
        renamed_df2 = df2.rename(columns=mapping2)
        # join the dataframes
        if len(left_on) == 0:
            df = renamed_df1.merge(renamed_df2, how="cross")
        else:
            df = renamed_df1.merge(
                renamed_df2,
                left_on=[mapping1[col] for col in left_on],
                right_on=[mapping2[col] for col in right_on],
            )
        # drop duplicate columns from the *right* dataframe
        df.drop(columns=[mapping2[col] for col in right_on], inplace=True)
        # construct the mapping functions from the columns of each dataframe to
        # the columns of the result
        from1 = mapping1
        from2 = {}
        for col in df2.columns:
            if mapping2[col] in df.columns:
                # first, assign the columns that we did not drop
                from2[col] = mapping2[col]
        for left_col, right_col in zip(left_on, right_on):
            # fill in the remaining ones
            from2[right_col] = mapping1[left_col]
        return df, from1, from2

    def merge(self, f1: FuncQuery, f2: FuncQuery):
        """
        Merge two func query nodes in the graph by joining their tables along
        the columns that correspond to the shared inputs/outputs
        """
        print("Its happening")
        # get the data
        df1, df2 = self.tables[f1], self.tables[f2]
        # compute correspondence between columns and vqs
        col_to_vq1, vq_to_cols1 = self._get_col_to_vq_mappings(f1)
        col_to_vq2, vq_to_cols2 = self._get_col_to_vq_mappings(f2)
        # apply self-join constraints
        df1, col_to_vq1, vq_to_col1 = self._drop_self_constraints(df1, vq_to_cols1)
        df2, col_to_vq2, vq_to_col2 = self._drop_self_constraints(df2, vq_to_cols2)
        # compute the pairs of columns along which we need to join
        # {shared value query: (col1, col2)}
        intersection_vq_to_col_pairs = OrderedDict({})
        for col, vq in col_to_vq1.items():
            if vq in col_to_vq2.values():
                intersection_vq_to_col_pairs[vq] = (col, vq_to_col2[vq])
        left_on = [col for _, (col, _) in intersection_vq_to_col_pairs.items()]
        right_on = [col for _, (_, col) in intersection_vq_to_col_pairs.items()]
        df, from1, from2 = self._join_dataframes(
            df1=df1, df2=df2, left_on=left_on, right_on=right_on
        )
        # get the correspondence between columns and vqs for the new table
        inputs = {}
        for col in df.columns:
            if col in from1.values():
                col_1 = invert_dict(from1)[col]
                inputs[col] = col_to_vq1[col_1]
            elif col in from2.values():
                col_2 = invert_dict(from2)[col]
                inputs[col] = col_to_vq2[col_2]
            else:
                raise ValueError()
        # insert new func query
        f = FuncQuery(inputs=inputs, func_op=None)
        for k, v in inputs.items():
            v.add_consumer(consumer=f, consumed_as=k)
        self.tables[f] = df
        self.func_queries.append(f)
        # remove the old func queries from the graph
        f1.unlink()
        f2.unlink()
        self.func_queries = [f for f in self.func_queries if f not in (f1, f2)]
        del self.tables[f1], self.tables[f2]

    ### solver and solver utils
    def compute_intersection_size(self, f1: FuncQuery, f2: FuncQuery) -> int:
        col_to_vq1, vq_to_cols1 = self._get_col_to_vq_mappings(f1)
        col_to_vq2, vq_to_cols2 = self._get_col_to_vq_mappings(f2)
        return len(set(col_to_vq1.values()) & set(col_to_vq2.values()))

    def solve(self, select_vqs: List[ValQuery]) -> pd.DataFrame:
        while len(self.func_queries) > 1:
            intersections = {}
            # compute pairwise intersections
            for f1 in self.func_queries:
                for f2 in self.func_queries:
                    if f1 == f2:
                        continue
                    intersections[(f1, f2)] = self.compute_intersection_size(f1, f2)
            # pick the pair with the largest intersection
            f1, f2 = max(intersections, key=lambda x: intersections.get(x, 0))
            # merge the pair
            self.merge(f1, f2)
        assert len(self.tables) == 1
        df = self.tables[self.func_queries[0]]
        f = self.func_queries[0]
        # figure out which columns to select
        col_to_vq, vq_to_cols = self._get_col_to_vq_mappings(f)
        cols = [vq_to_cols[vq][0] for vq in select_vqs]
        return df[cols]

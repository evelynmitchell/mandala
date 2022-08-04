from mandala_lite.all import *
from mandala_lite.tests.utils import *


def test_rel_storage():
    rel_storage = DuckDBRelStorage()
    assert set(rel_storage.get_tables()) == set()
    rel_storage.create_relation(
        name="test", columns=[("a", None), ("b", None)], primary_key="a"
    )
    assert set(rel_storage.get_tables()) == {"test"}
    assert rel_storage.get_data(table="test").empty
    df = pd.DataFrame({"a": ["x", "y"], "b": ["z", "w"]})
    ta = pa.Table.from_pandas(df)
    rel_storage.insert(relation="test", ta=ta)
    assert (rel_storage.get_data(table="test") == df).all().all()
    rel_storage.upsert(relation="test", ta=ta)
    assert (rel_storage.get_data(table="test") == df).all().all()
    rel_storage.create_column(relation="test", name="c", default_value="a")
    df["c"] = ["a", "a"]
    assert (rel_storage.get_data(table="test") == df).all().all()
    rel_storage.delete(relation="test", index=["x", "y"])
    assert rel_storage.get_data(table="test").empty


def test_main_storage():
    storage = Storage()


def test_signatures():
    sig = Signature(
        ui_name="f",
        input_names={"x", "y"},
        n_outputs=1,
        defaults={"y": 42},
        version=0,
    )

    # if internal data has not been set, it should not be accessible
    try:
        sig.internal_name
        assert False
    except ValueError:
        assert True

    try:
        sig.ui_to_internal_input_map
        assert False
    except ValueError:
        assert True

    with_internal = sig._generate_internal()
    assert not sig.has_internal_data
    assert with_internal.has_internal_data

    ### invalid signature changes
    # remove input
    new = Signature(
        ui_name="f",
        input_names={"x", "z"},
        n_outputs=1,
        defaults={"y": 42},
        version=0,
    )
    try:
        sig.update(new=new)
        assert False
    except ValueError:
        assert True
    # change number of outputs
    new = Signature(
        ui_name="f",
        input_names={"x", "y"},
        n_outputs=2,
        defaults={"y": 42},
        version=0,
    )
    try:
        sig.update(new=new)
        assert False
    except ValueError:
        assert True
    # remove default
    new = Signature(
        ui_name="f",
        input_names={"x", "y"},
        n_outputs=1,
        defaults={},
        version=0,
    )
    try:
        sig.update(new=new)
        assert False
    except ValueError:
        assert True
    # change version
    new = Signature(
        ui_name="f",
        input_names={"x", "y"},
        n_outputs=1,
        defaults={"y": 42},
        version=1,
    )
    try:
        sig.update(new=new)
        assert False
    except ValueError:
        assert True

    # add input
    sig = sig._generate_internal()
    try:
        sig.create_input(name="y", default=23)
        assert False
    except ValueError:
        assert True
    new = sig.create_input(name="z", default=23)
    assert new.input_names == {"x", "y", "z"}

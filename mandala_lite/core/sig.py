from ..common_imports import *
from .config import Config
from .utils import get_uid, Hashing, is_subdict
from ..utils import serialize, deserialize


class Signature:
    """
    Holds and manipulates the relevant metadata for a memoized function, which
    includes
        - the function's user-interface (human-facing) and internal (used by storage)
        name,
        - the user-interface and internal input names (and the mapping between them),
        - the version,
        - and the default values.
        - (optional) superop status

    Responsible for manipulations to this state, and keeping it consistent, so
    e.g. logic for checking if a refactoring makes sense should be hidden here.

    The internal name of the function is an immutable UID that is used to
    identify the function throughout its entire lifetime for the storage it is
    connected to. The UI name is what the function is named in the source
    code, and can be changed. Same for the internal/UI input names.

    What goes through most of the system at runtime are the UI names, to make it
    easier to debug and inspect things. The internal names are used only in very
    specific and isolated parts of the architecture.
    """

    def __init__(
        self,
        ui_name: str,
        input_names: Set[str],
        n_outputs: int,
        defaults: Dict[str, Any],
        version: int,
    ):
        self.ui_name = ui_name
        self.input_names = input_names
        self.defaults = defaults
        self.n_outputs = n_outputs
        self.version = version
        self._internal_name = None
        # ui name -> internal name for inputs
        self._ui_to_internal_input_map = None
        # internal input name -> UID of default value
        # this stores the UIDs of default values for inputs that have been
        # added to the function since its creation
        self._new_input_defaults_uids = {}

    def __repr__(self) -> str:
        return (
            f"Signature(ui_name={self.ui_name}, input_names={self.input_names}, "
            f"n_outputs={self.n_outputs}, defaults={self.defaults}, "
            f"version={self.version}, internal_name={self._internal_name}, "
            f"ui_to_internal_input_map={self._ui_to_internal_input_map}, "
            f"new_input_defaults_uids={self._new_input_defaults_uids})"
        )

    @property
    def versioned_ui_name(self) -> str:
        """
        Return the version-qualified human-readable name of this signature, used to
        disambiguate between different versions of the same function.
        """
        return f"{self.ui_name}_{self.version}"

    @property
    def versioned_internal_name(self) -> str:
        """
        Return the version-qualified internal name of this signature
        """
        return f"{self.internal_name}_{self.version}"

    @property
    def internal_name(self) -> str:
        if self._internal_name is None:
            raise ValueError("Internal name not set")
        return self._internal_name

    @staticmethod
    def parse_versioned_name(versioned_name: str) -> Tuple[str, int]:
        """
        Recover the name and version from a version-qualified name
        """
        name, version_string = versioned_name.rsplit("_", 1)
        return name, int(version_string)

    @property
    def ui_to_internal_input_map(self) -> Dict[str, str]:
        if self._ui_to_internal_input_map is None:
            raise ValueError("Internal input names not set")
        return self._ui_to_internal_input_map

    @property
    def internal_to_ui_input_map(self) -> Dict[str, str]:
        """
        Mapping from internal input names to their UI names
        """
        if not self.has_internal_data:
            raise ValueError()
        return {v: k for k, v in self.ui_to_internal_input_map.items()}

    @property
    def has_internal_data(self) -> bool:
        """
        Whether this signature has had its internal data (internal signature
        name and internal input names) set.
        """
        return (
            self._internal_name is not None
            and self._ui_to_internal_input_map is not None
            and self._ui_to_internal_input_map.keys() == self.input_names
        )

    @property
    def internal_input_names(self) -> Set[str]:
        return set(self.ui_to_internal_input_map.values())

    def __eq__(self, other: Any) -> bool:
        return (
            isinstance(other, Signature)
            and self.ui_name == other.ui_name
            and self.input_names == other.input_names
            and self.n_outputs == other.n_outputs
            and self.defaults == other.defaults
            and self.version == other.version
            and self._internal_name == other._internal_name
            and self._ui_to_internal_input_map == other._ui_to_internal_input_map
            and self._new_input_defaults_uids == other._new_input_defaults_uids
        )
    
    ############################################################################
    ### PURE methods for manipulating the signature
    ### to avoid broken state
    ############################################################################
    def _generate_internal(self, internal_name: Optional[str] = None) -> "Signature":
        """
        Assign internal names to random UIDs.

        Providing `internal_name` explicitly can be used to set the same
        internal name for different versions of the same function.
        """
        res = copy.deepcopy(self)
        if internal_name is None:
            internal_name = get_uid()
        res._internal_name, res._ui_to_internal_input_map = internal_name, {
            k: get_uid() for k in self.input_names
        }
        return res

    def is_compatible(self, new: "Signature") -> Tuple[bool, Optional[str]]:
        """
        Check if a new signature (possibly without internal data) is compatible
        with this signature.

        Currently, the only way to be compatible is to be either the same object
        or an extension with new arguments.

        Returns:
            Tuple[bool, str]: (outcome, (reason if `False`, None if True))
        """
        if new.version != self.version:
            return False, "Versions do not match"
        if new.ui_name != self.ui_name:
            return False, "UI names do not match"
        if new.has_internal_data and self.has_internal_data:
            if new.internal_name != self.internal_name:
                return False, "Internal names do not match"
            if not is_subdict(
                self.ui_to_internal_input_map, new.ui_to_internal_input_map
            ):
                return False, "UI -> internal input mapping is inconsistent"
        if not set.issubset(set(self.input_names), set(new.input_names)):
            return False, "Removing inputs is not supported"
        if not self.n_outputs == new.n_outputs:
            return False, "Changing the number of outputs is not supported"
        if not is_subdict(self.defaults, new.defaults):
            return False, "New defaults are inconsistent with current defaults"
        if not is_subdict(self._new_input_defaults_uids, new._new_input_defaults_uids):
            return False, "New default UIDs are inconsistent with current default UIDs"
        for k in new.input_names:
            if k not in self.input_names:
                if k not in new.defaults.keys():
                    return False, f"All new arguments must be created with defaults!"
        return True, None

    def update(self, new: "Signature") -> Tuple["Signature", dict]:
        """
        Return an updated version of this signature based on a new signature
        (possibly without internal data), plus a description of the updates.

        If the new signature has internal data, it is copied over.

        NOTE: the new signature need not have internal data. The goal of this
        method is to be able to update from a function provided by the user that
        has not been synchronized yet.

        This takes care of
            - checking that the new signature is compatible with the old one
            - generating names for new inputs, if any.

        Returns:
            - new `Signature` object
            - a dictionary of {new ui input name: default value} for any new inputs
              that were created
        """
        is_compatible, reason = self.is_compatible(new)
        if not is_compatible:
            raise ValueError(reason)
        new_defaults = new.defaults
        new_sig = copy.deepcopy(self)
        updates = {}
        for k in new.input_names:
            if k not in new_sig.input_names:
                # this means a new input is being created
                if new.has_internal_data:
                    internal_name = new.ui_to_internal_input_map[k]
                else:
                    internal_name = None
                new_sig = new_sig.create_input(
                    name=k, default=new_defaults[k], internal_name=internal_name
                )
                updates[k] = new_defaults[k]
        return new_sig, updates

    def create_input(
        self, name: str, default, internal_name: Optional[str] = None
    ) -> "Signature":
        """
        Add an input with a default value to this signature. This takes care of
        all the internal bookkeeping, including figuring out the UID for the
        default value.
        """
        if name in self.input_names:
            raise ValueError(f'Input "{name}" already exists')
        if not self.has_internal_data:
            raise ValueError("Cannot add inputs to a signature without internal data")
        res = copy.deepcopy(self)
        res.input_names.add(name)
        internal_name = get_uid() if internal_name is None else internal_name
        res.ui_to_internal_input_map[name] = internal_name
        res.defaults[name] = default
        #! if we implement custom types, this will need to be updated
        default_uid = Hashing.get_content_hash(obj=default)
        res._new_input_defaults_uids[internal_name] = default_uid
        return res

    def rename(self, new_name: str) -> "Signature":
        """
        Change the ui name
        """
        res = copy.deepcopy(self)
        res.ui_name = new_name
        return res

    def rename_inputs(self, mapping: Dict[str, str]) -> "Signature":
        """
        Change UI names according to the given mapping.

        Supporting only a method that changes multiple names at once is more
        convenient, since we must support applying updates in bulk anyway.
        """
        assert all(k in self.input_names for k in mapping.keys())
        current_names = list(self.input_names)
        new_names = [mapping.get(k, k) for k in current_names]
        if len(set(new_names)) != len(new_names):
            raise ValueError("Input name collision")
        res = copy.deepcopy(self)
        for current_name in mapping.keys():
            res.input_names.remove(current_name)
        for new_name in mapping.values():
            res.input_names.add(new_name)
        for current_name, new_name in mapping.items():
            res.ui_to_internal_input_map[new_name] = res.ui_to_internal_input_map.pop(
                current_name
            )
        return res

    @staticmethod
    def from_py(
        name: str,
        version: int,
        sig: inspect.Signature,
    ) -> "Signature":
        """
        Create a `Signature` from a Python function's signature and the other
        necessary metadata, and check it satisfies mandala-specific constraints.
        """
        input_names = set(
            [
                param.name
                for param in sig.parameters.values()
                if param.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
            ]
        )
        # ensure that there will be no collisions with input and output names
        if any(name.startswith(Config.output_name_prefix) for name in input_names):
            raise ValueError(
                f"Input names cannot start with {Config.output_name_prefix}"
            )
        return_annotation = sig.return_annotation
        if (
            hasattr(return_annotation, "__origin__")
            and return_annotation.__origin__ is tuple
        ):
            n_outputs = len(return_annotation.__args__)
        elif return_annotation is inspect._empty:
            n_outputs = 0
        else:
            n_outputs = 1
        defaults = {
            param.name: param.default
            for param in sig.parameters.values()
            if param.default is not inspect.Parameter.empty
        }
        return Signature(
            ui_name=name,
            input_names=input_names,
            n_outputs=n_outputs,
            defaults=defaults,
            version=version,
        )

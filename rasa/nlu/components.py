import itertools
import logging
import typing
from typing import Any, Dict, Hashable, List, Optional, Set, Text, Tuple, Type, Iterable

from rasa.constants import DOCS_URL_MIGRATION_GUIDE
from rasa.nlu.constants import TRAINABLE_EXTRACTORS
from rasa.nlu.config import RasaNLUModelConfig, override_defaults, InvalidConfigError
from rasa.nlu.training_data import Message, TrainingData
from rasa.utils.common import raise_warning

if typing.TYPE_CHECKING:
    from rasa.nlu.model import Metadata

logger = logging.getLogger(__name__)


def find_unavailable_packages(package_names: List[Text]) -> Set[Text]:
    """Tries to import all package names and returns the packages where it failed.

    Args:
        package_names: The package names to import.

    Returns:
        Package names that could not be imported.
    """

    import importlib

    failed_imports = set()
    for package in package_names:
        try:
            importlib.import_module(package)
        except ImportError:
            failed_imports.add(package)
    return failed_imports


def validate_requirements(component_names: List[Text]) -> None:
    """Validates that all required importable python packages are installed.

    Args:
        component_names: The list of component names.
    """

    from rasa.nlu import registry

    # Validate that all required packages are installed
    failed_imports = set()
    for component_name in component_names:
        component_class = registry.get_component_class(component_name)
        failed_imports.update(
            find_unavailable_packages(component_class.required_packages())
        )
    if failed_imports:  # pragma: no cover
        # if available, use the development file to figure out the correct
        # version numbers for each requirement
        raise Exception(
            f"Not all required importable packages are installed. "
            f"To use this pipeline, you need to install the "
            f"missing dependencies. "
            f"Please install the package(s) that contain the module(s): "
            f"{', '.join(failed_imports)}"
        )


def validate_empty_pipeline(pipeline: List["Component"]) -> None:
    """Ensures the pipeline is not empty.

    Args:
        pipeline: the list of the :class:`rasa.nlu.components.Component`.
    """

    if len(pipeline) == 0:
        raise InvalidConfigError(
            "Can not train an empty pipeline. "
            "Make sure to specify a proper pipeline in "
            "the configuration using the 'pipeline' key. "
            "The 'backend' configuration key is "
            "NOT supported anymore."
        )


def validate_only_one_tokenizer_is_used(pipeline: List["Component"]) -> None:
    """Validates that only one tokenizer is present in the pipeline.

    Args:
        pipeline: the list of the :class:`rasa.nlu.components.Component`.
    """

    from rasa.nlu.tokenizers.tokenizer import Tokenizer

    tokenizer_names = []
    for component in pipeline:
        if isinstance(component, Tokenizer):
            tokenizer_names.append(component.name)

    if len(tokenizer_names) > 1:
        raise InvalidConfigError(
            f"More than one tokenizer is used: {tokenizer_names}. "
            f"You can use only one tokenizer."
        )


def _required_component_in_pipeline(
    required_component: Type["Component"], pipeline: List["Component"]
) -> bool:
    """Checks that required component present in the pipeline.

    Args:
        required_component: A class name of the required component.
        pipeline: The list of the :class:`rasa.nlu.components.Component`.

    Returns:
        `True` if required_component is in the pipeline, `False` otherwise.
    """

    for previous_component in pipeline:
        if isinstance(previous_component, required_component):
            return True
    return False


def _check_deprecated_attributes(component: "Component") -> None:
    """Checks that the component doesn't have deprecated attributes.

    Args:
        component: The :class:`rasa.nlu.components.Component`.
    """

    if hasattr(component, "provides"):
        raise_warning(
            f"'{component.name}' contains property 'provides', "
            f"which is deprecated. There is no need to specify "
            f"the list of attributes that a component provides.",
            category=FutureWarning,
            docs=DOCS_URL_MIGRATION_GUIDE,
        )
    if hasattr(component, "requires"):
        raise_warning(
            f"'{component.name}' contains property 'requires', "
            f"which is deprecated. Use 'required_components()' method "
            f"to specify which components are required to be present "
            f"in the pipeline by this component.",
            category=FutureWarning,
            docs=DOCS_URL_MIGRATION_GUIDE,
        )


def validate_required_components(pipeline: List["Component"]) -> None:
    """Validates that all required components are present in the pipeline.

    Args:
        pipeline: The list of the :class:`rasa.nlu.components.Component`.
    """

    for i, component in enumerate(pipeline):
        _check_deprecated_attributes(component)

        missing_components = []
        for required_component in component.required_components():
            if not _required_component_in_pipeline(required_component, pipeline[:i]):
                missing_components.append(required_component.name)

        if missing_components:
            raise InvalidConfigError(
                f"'{component.name}' requires {missing_components}. "
                f"Add required components to the pipeline."
            )


def validate_pipeline(pipeline: List["Component"]) -> None:
    """Validates the pipeline.

    Args:
        pipeline: The list of the :class:`rasa.nlu.components.Component`.
    """

    validate_empty_pipeline(pipeline)
    validate_only_one_tokenizer_is_used(pipeline)
    validate_required_components(pipeline)


def any_components_in_pipeline(components: Iterable[Text], pipeline: List["Component"]):
    """Check if any of the provided components are listed in the pipeline.

    Args:
        components: A list of :class:`rasa.nlu.components.Component`s to check.
        pipeline: A list of :class:`rasa.nlu.components.Component`s.

    Returns:
        `True` if any of the `components` are in the `pipeline`, else `False`.

    """
    return any(any(component.name == c for component in pipeline) for c in components)


def validate_required_components_from_data(
    pipeline: List["Component"], data: TrainingData
) -> None:
    """Validates that all components are present in the pipeline based on data.

    Args:
        pipeline: The list of the :class:`rasa.nlu.components.Component`s.
        data: The :class:`rasa.nlu.training_data.training_data.TrainingData`.
    """

    if data.response_examples and not any_components_in_pipeline(
        ["ResponseSelector"], pipeline
    ):
        raise_warning(
            "You have defined training data with examples for training a response "
            "selector, but your NLU pipeline does not include a response selector "
            "component. To train a model on your response selector data, add a "
            "'ResponseSelector' to your pipeline."
        )

    if data.entity_examples and not any_components_in_pipeline(
        TRAINABLE_EXTRACTORS, pipeline
    ):
        raise_warning(
            "You have defined training data consisting of entity examples, but "
            "your NLU pipeline does not include an entity extractor trained on "
            "your training data. To extract non-pretrained entities, add one of "
            f"{TRAINABLE_EXTRACTORS} to your pipeline."
        )

    if data.entity_examples and not any_components_in_pipeline(
        {"DIETClassifier", "CRFEntityExtractor"}, pipeline
    ):
        if data.entity_roles_groups_used():
            raise_warning(
                "You have defined training data with entities that have roles/groups, "
                "but your NLU pipeline does not include a 'DIETClassifier' or a "
                "'CRFEntityExtractor'. To train entities that have roles/groups, "
                "add either 'DIETClassifier' or 'CRFEntityExtractor' to your "
                "pipeline."
            )

    if data.regex_features and not any_components_in_pipeline(
        ["RegexFeaturizer", "RegexEntityExtractor"], pipeline
    ):
        raise_warning(
            "You have defined training data with regexes, but "
            "your NLU pipeline does not include a 'RegexFeaturizer' or a "
            "'RegexEntityExtractor'. To use regexes, include either a "
            "'RegexFeaturizer' or a 'RegexEntityExtractor' in your pipeline."
        )

    if data.lookup_tables and not any_components_in_pipeline(
        ["RegexFeaturizer", "RegexEntityExtractor"], pipeline
    ):
        raise_warning(
            "You have defined training data consisting of lookup tables, but "
            "your NLU pipeline does not include a 'RegexFeaturizer' or a "
            "'RegexEntityExtractor'. To use lookup tables, include either a "
            "'RegexFeaturizer' or a 'RegexEntityExtractor' in your pipeline."
        )

    if data.lookup_tables:
        if not any_components_in_pipeline(
            ["CRFEntityExtractor", "DIETClassifier"], pipeline
        ):
            raise_warning(
                "You have defined training data consisting of lookup tables, but "
                "your NLU pipeline does not include any components that use these "
                "features. To make use of lookup tables, add a 'DIETClassifier' or a "
                "'CRFEntityExtractor' with the 'pattern' feature to your pipeline."
            )
        elif any_components_in_pipeline(["CRFEntityExtractor"], pipeline):
            crf_components = [c for c in pipeline if c.name == "CRFEntityExtractor"]
            # check to see if any of the possible CRFEntityExtractors will
            # featurize `pattern`
            has_pattern_feature = False
            for crf in crf_components:
                crf_features = crf.component_config.get("features")
                # iterate through [[before],[word],[after]] features
                has_pattern_feature = "pattern" in itertools.chain(*crf_features)

            if not has_pattern_feature:
                raise_warning(
                    "You have defined training data consisting of lookup tables, but "
                    "your NLU pipeline's 'CRFEntityExtractor' does not include the "
                    "'pattern' feature. To featurize lookup tables, add the 'pattern' "
                    "feature to the 'CRFEntityExtractor' in your pipeline."
                )

    if data.entity_synonyms and not any_components_in_pipeline(
        ["EntitySynonymMapper"], pipeline
    ):
        raise_warning(
            "You have defined synonyms in your training data, but "
            "your NLU pipeline does not include an 'EntitySynonymMapper'. "
            "To map synonyms, add an 'EntitySynonymMapper' to your pipeline."
        )


class MissingArgumentError(ValueError):
    """Raised when not all parameters can be filled from the context / config.

    Attributes:
        message -- explanation of which parameter is missing
    """

    def __init__(self, message: Text) -> None:
        super().__init__(message)
        self.message = message

    def __str__(self) -> Text:
        return self.message


class UnsupportedLanguageError(Exception):
    """Raised when a component is created but the language is not supported.

    Attributes:
        component -- component name
        language -- language that component doesn't support
    """

    def __init__(self, component: Text, language: Text) -> None:
        self.component = component
        self.language = language

        super().__init__(component, language)

    def __str__(self) -> Text:
        return (
            f"component '{self.component}' does not support language '{self.language}'."
        )


class ComponentMetaclass(type):
    """Metaclass with `name` class property."""

    @property
    def name(cls):
        """The name property is a function of the class - its __name__."""

        return cls.__name__


class Component(metaclass=ComponentMetaclass):
    """A component is a message processing unit in a pipeline.

    Components are collected sequentially in a pipeline. Each component
    is called one after another. This holds for
    initialization, training, persisting and loading the components.
    If a component comes first in a pipeline, its
    methods will be called first.

    E.g. to process an incoming message, the ``process`` method of
    each component will be called. During the processing
    (as well as the training, persisting and initialization)
    components can pass information to other components.
    The information is passed to other components by providing
    attributes to the so called pipeline context. The
    pipeline context contains all the information of the previous
    components a component can use to do its own
    processing. For example, a featurizer component can provide
    features that are used by another component down
    the pipeline to do intent classification.
    """

    # Component class name is used when integrating it in a
    # pipeline. E.g. ``[ComponentA, ComponentB]``
    # will be a proper pipeline definition where ``ComponentA``
    # is the name of the first component of the pipeline.
    @property
    def name(self):
        """Access the class's property name from an instance."""

        return type(self).name

    # Which components are required by this component.
    # Listed components should appear before the component itself in the pipeline.
    @classmethod
    def required_components(cls) -> List[Type["Component"]]:
        """Specify which components need to be present in the pipeline.

        Returns:
            The list of class names of required components.
        """

        return []

    # Defines the default configuration parameters of a component
    # these values can be overwritten in the pipeline configuration
    # of the model. The component should choose sensible defaults
    # and should be able to create reasonable results with the defaults.
    defaults = {}

    # Defines what language(s) this component can handle.
    # This attribute is designed for instance method: `can_handle_language`.
    # Default value is None which means it can handle all languages.
    # This is an important feature for backwards compatibility of components.
    supported_language_list = None

    # Defines what language(s) this component can NOT handle.
    # This attribute is designed for instance method: `can_handle_language`.
    # Default value is None which means it can handle all languages.
    # This is an important feature for backwards compatibility of components.
    not_supported_language_list = None

    def __init__(self, component_config: Optional[Dict[Text, Any]] = None) -> None:

        if not component_config:
            component_config = {}

        # makes sure the name of the configuration is part of the config
        # this is important for e.g. persistence
        component_config["name"] = self.name

        self.component_config = override_defaults(self.defaults, component_config)

        self.partial_processing_pipeline = None
        self.partial_processing_context = None

    @classmethod
    def required_packages(cls) -> List[Text]:
        """Specify which python packages need to be installed.

        E.g. ``["spacy"]``. More specifically, these should be
        importable python package names e.g. `sklearn` and not package
        names in the dependencies sense e.g. `scikit-learn`

        This list of requirements allows us to fail early during training
        if a required package is not installed.

        Returns:
            The list of required package names.
        """

        return []

    @classmethod
    def load(
        cls,
        meta: Dict[Text, Any],
        model_dir: Optional[Text] = None,
        model_metadata: Optional["Metadata"] = None,
        cached_component: Optional["Component"] = None,
        **kwargs: Any,
    ) -> "Component":
        """Load this component from file.

        After a component has been trained, it will be persisted by
        calling `persist`. When the pipeline gets loaded again,
        this component needs to be able to restore itself.
        Components can rely on any context attributes that are
        created by :meth:`components.Component.create`
        calls to components previous to this one.

        Args:
            meta: Any configuration parameter related to the model.
            model_dir: The directory to load the component from.
            model_metadata: The model's :class:`rasa.nlu.model.Metadata`.
            cached_component: The cached component.

        Returns:
            the loaded component
        """

        if cached_component:
            return cached_component

        return cls(meta)

    @classmethod
    def create(
        cls, component_config: Dict[Text, Any], config: RasaNLUModelConfig
    ) -> "Component":
        """Creates this component (e.g. before a training is started).

        Method can access all configuration parameters.

        Args:
            component_config: The components configuration parameters.
            config: The model configuration parameters.

        Returns:
            The created component.
        """

        # Check language supporting
        language = config.language
        if not cls.can_handle_language(language):
            # check failed
            raise UnsupportedLanguageError(cls.name, language)

        return cls(component_config)

    def provide_context(self) -> Optional[Dict[Text, Any]]:
        """Initialize this component for a new pipeline.

        This function will be called before the training
        is started and before the first message is processed using
        the interpreter. The component gets the opportunity to
        add information to the context that is passed through
        the pipeline during training and message parsing. Most
        components do not need to implement this method.
        It's mostly used to initialize framework environments
        like MITIE and spacy
        (e.g. loading word vectors for the pipeline).

        Returns:
            The updated component configuration.
        """

        pass

    def train(
        self,
        training_data: TrainingData,
        config: Optional[RasaNLUModelConfig] = None,
        **kwargs: Any,
    ) -> None:
        """Train this component.

        This is the components chance to train itself provided
        with the training data. The component can rely on
        any context attribute to be present, that gets created
        by a call to :meth:`rasa.nlu.components.Component.create`
        of ANY component and
        on any context attributes created by a call to
        :meth:`rasa.nlu.components.Component.train`
        of components previous to this one.

        Args:
            training_data:
                The :class:`rasa.nlu.training_data.training_data.TrainingData`.
            config: The model configuration parameters.

        """

        pass

    def process(self, message: Message, **kwargs: Any) -> None:
        """Process an incoming message.

        This is the components chance to process an incoming
        message. The component can rely on
        any context attribute to be present, that gets created
        by a call to :meth:`rasa.nlu.components.Component.create`
        of ANY component and
        on any context attributes created by a call to
        :meth:`rasa.nlu.components.Component.process`
        of components previous to this one.

        Args:
            message: The :class:`rasa.nlu.training_data.message.Message` to process.

        """

        pass

    def persist(self, file_name: Text, model_dir: Text) -> Optional[Dict[Text, Any]]:
        """Persist this component to disk for future loading.

        Args:
            file_name: The file name of the model.
            model_dir: The directory to store the model to.

        Returns:
            An optional dictionary with any information about the stored model.
        """

        pass

    @classmethod
    def cache_key(
        cls, component_meta: Dict[Text, Any], model_metadata: "Metadata"
    ) -> Optional[Text]:
        """This key is used to cache components.

        If a component is unique to a model it should return None.
        Otherwise, an instantiation of the
        component will be reused for all models where the
        metadata creates the same key.

        Args:
            component_meta: The component configuration.
            model_metadata: The component's :class:`rasa.nlu.model.Metadata`.

        Returns:
            A unique caching key.
        """

        return None

    def __getstate__(self) -> Any:
        d = self.__dict__.copy()
        # these properties should not be pickled
        if "partial_processing_context" in d:
            del d["partial_processing_context"]
        if "partial_processing_pipeline" in d:
            del d["partial_processing_pipeline"]
        return d

    def __eq__(self, other) -> bool:
        return self.__dict__ == other.__dict__

    def prepare_partial_processing(
        self, pipeline: List["Component"], context: Dict[Text, Any]
    ) -> None:
        """Sets the pipeline and context used for partial processing.

        The pipeline should be a list of components that are
        previous to this one in the pipeline and
        have already finished their training (and can therefore
        be safely used to process messages).

        Args:
            pipeline: The list of components.
            context: The context of processing.

        """

        self.partial_processing_pipeline = pipeline
        self.partial_processing_context = context

    def partially_process(self, message: Message) -> Message:
        """Allows the component to process messages during
        training (e.g. external training data).

        The passed message will be processed by all components
        previous to this one in the pipeline.

        Args:
            message: The :class:`rasa.nlu.training_data.message.Message` to process.

        Returns:
            The processed :class:`rasa.nlu.training_data.message.Message`.

        """

        if self.partial_processing_context is not None:
            for component in self.partial_processing_pipeline:
                component.process(message, **self.partial_processing_context)
        else:
            logger.info("Failed to run partial processing due to missing pipeline.")
        return message

    @classmethod
    def can_handle_language(cls, language: Hashable) -> bool:
        """Check if component supports a specific language.

        This method can be overwritten when needed. (e.g. dynamically
        determine which language is supported.)

        Args:
            language: The language to check.

        Returns:
            `True` if component can handle specific language, `False` otherwise.
        """

        # if language_list is set to `None` it means: support all languages
        if language is None or (
            cls.supported_language_list is None
            and cls.not_supported_language_list is None
        ):
            return True

        language_list = cls.supported_language_list or []
        not_supported_language_list = cls.not_supported_language_list or []

        return language in language_list or language not in not_supported_language_list


C = typing.TypeVar("C", bound=Component)


class ComponentBuilder:
    """Creates trainers and interpreters based on configurations.

    Caches components for reuse.
    """

    def __init__(self, use_cache: bool = True) -> None:
        self.use_cache = use_cache
        # Reuse nlp and featurizers where possible to save memory,
        # every component that implements a cache-key will be cached
        self.component_cache = {}

    def __get_cached_component(
        self, component_meta: Dict[Text, Any], model_metadata: "Metadata"
    ) -> Tuple[Optional[Component], Optional[Text]]:
        """Load a component from the cache, if it exists.

        Returns the component, if found, and the cache key.
        """

        from rasa.nlu import registry

        # try to get class name first, else create by name
        component_name = component_meta.get("class", component_meta["name"])
        component_class = registry.get_component_class(component_name)
        cache_key = component_class.cache_key(component_meta, model_metadata)
        if (
            cache_key is not None
            and self.use_cache
            and cache_key in self.component_cache
        ):
            return self.component_cache[cache_key], cache_key

        return None, cache_key

    def __add_to_cache(self, component: Component, cache_key: Optional[Text]) -> None:
        """Add a component to the cache."""

        if cache_key is not None and self.use_cache:
            self.component_cache[cache_key] = component
            logger.info(
                f"Added '{component.name}' to component cache. Key '{cache_key}'."
            )

    def load_component(
        self,
        component_meta: Dict[Text, Any],
        model_dir: Text,
        model_metadata: "Metadata",
        **context: Any,
    ) -> Component:
        """Loads a component.

        Tries to retrieve a component from the cache, else calls
        ``load`` to create a new component.

        Args:
            component_meta:
                The metadata of the component to load in the pipeline.
            model_dir:
                The directory to read the model from.
            model_metadata (Metadata):
                The model's :class:`rasa.nlu.model.Metadata`.

        Returns:
            The loaded component.
        """

        from rasa.nlu import registry

        try:
            cached_component, cache_key = self.__get_cached_component(
                component_meta, model_metadata
            )
            component = registry.load_component_by_meta(
                component_meta, model_dir, model_metadata, cached_component, **context
            )
            if not cached_component:
                # If the component wasn't in the cache,
                # let us add it if possible
                self.__add_to_cache(component, cache_key)
            return component
        except MissingArgumentError as e:  # pragma: no cover
            raise Exception(
                f"Failed to load component from file '{component_meta.get('file')}'. "
                f"Error: {e}"
            )

    def create_component(
        self, component_config: Dict[Text, Any], cfg: RasaNLUModelConfig
    ) -> Component:
        """Creates a component.

        Tries to retrieve a component from the cache,
        calls `create` to create a new component.

        Args:
            component_config: The component configuration.
            cfg: The model configuration.

        Returns:
            The created component.
        """

        from rasa.nlu import registry
        from rasa.nlu.model import Metadata

        try:
            component, cache_key = self.__get_cached_component(
                component_config, Metadata(cfg.as_dict(), None)
            )
            if component is None:
                component = registry.create_component_by_config(component_config, cfg)
                self.__add_to_cache(component, cache_key)
            return component
        except MissingArgumentError as e:  # pragma: no cover
            raise Exception(
                f"Failed to create component '{component_config['name']}'. "
                f"Error: {e}"
            )

    def create_component_from_class(self, component_class: Type[C], **cfg: Any) -> C:
        """Create a component based on a class and a configuration.

        Mainly used to make use of caching when instantiating component classes."""

        component_config = {"name": component_class.name}

        return self.create_component(component_config, RasaNLUModelConfig(cfg))

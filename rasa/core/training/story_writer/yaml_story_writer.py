from collections import OrderedDict
from pathlib import Path

import ruamel.yaml as ruamel_yaml
from typing import List, Text, Union, Optional

from rasa.utils.common import raise_warning
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from rasa.constants import LATEST_TRAINING_DATA_FORMAT_VERSION, DOCS_URL_STORIES
from rasa.core.events import UserUttered, ActionExecuted, SlotSet, Form
from rasa.core.training.story_reader.yaml_story_reader import (
    KEY_STORIES,
    KEY_STORY_NAME,
    KEY_USER_INTENT,
    KEY_ENTITIES,
    KEY_ACTION,
    KEY_STEPS,
    KEY_CHECKPOINT,
    KEY_SLOT_NAME,
    KEY_SLOT_VALUE,
    KEY_CHECKPOINT_SLOTS,
    KEY_OR,
)
from rasa.core.training.structures import StoryStep, Checkpoint

import rasa.utils.io as io_utils


class YAMLStoryWriter:
    """Writes Core training data into a file in a YAML format. """

    def dumps(self, story_steps: List[StoryStep]) -> Text:
        """Turns Story steps into a string.

        Args:
            story_steps: Original story steps to be converted to the YAML.

        Returns:
            String with story steps in the YAML format.
        """
        stream = ruamel_yaml.StringIO()
        self.dump(stream, story_steps)
        return stream.getvalue()

    def dump(
        self,
        target: Union[Text, Path, ruamel_yaml.StringIO],
        story_steps: List[StoryStep],
    ) -> None:
        """Writes Story steps into a target file/stream.

        Args:
            target: name of the target file/stream to write the YAML to.
            story_steps: Original story steps to be converted to the YAML.
        """
        from rasa.validator import KEY_TRAINING_DATA_FORMAT_VERSION

        self.target = target

        stories = []
        for story_step in story_steps:
            processed_story_step = self.process_story_step(story_step)
            if processed_story_step:
                stories.append(processed_story_step)

        result = OrderedDict()
        result[KEY_TRAINING_DATA_FORMAT_VERSION] = DoubleQuotedScalarString(
            LATEST_TRAINING_DATA_FORMAT_VERSION
        )
        result[KEY_STORIES] = stories

        io_utils.write_yaml(result, self.target, True)

    def process_story_step(self, story_step: StoryStep) -> Optional[OrderedDict]:
        """Converts a single story step into an ordered dict.

        Args:
            story_step: A single story step to be converted to the dict.

        Returns:
            Dict with a story step.
        """
        if self.story_contains_forms(story_step):
            raise_warning(
                f'File "{self.target}" contains a story "{story_step.block_name}" '
                f"that has form(s) in it. This story cannot be converted automatically "
                f"because of the new Rules system in Rasa Open Source "
                f"version {LATEST_TRAINING_DATA_FORMAT_VERSION}. "
                f"Please convert this story manually, it will be skipped now.",
                docs=DOCS_URL_STORIES,
            )
            return None

        result = OrderedDict()
        result[KEY_STORY_NAME] = story_step.block_name
        steps = self.process_checkpoints(story_step.start_checkpoints)

        for event in story_step.events:
            if isinstance(event, list):
                utterances = self.process_or_utterances(event)
                steps.append(utterances)
            elif isinstance(event, UserUttered):
                utterances = self.process_user_utterance(event)
                steps.append(utterances)
            elif isinstance(event, ActionExecuted):
                steps.append(self.process_action(event))
            elif isinstance(event, SlotSet):
                steps.append(self.process_slot(event))

        steps.extend(self.process_checkpoints(story_step.end_checkpoints))

        result[KEY_STEPS] = steps

        return result

    @staticmethod
    def story_contains_forms(story_step) -> bool:
        """Checks if the story step contains form actions.

        Args:
            story_step: A single story step.

        Returns:
            `True` if the `story_step` contains at least one form action,
            `False` otherwise.
        """
        return any([event for event in story_step.events if isinstance(event, Form)])

    @staticmethod
    def process_user_utterance(user_utterance: UserUttered) -> OrderedDict:
        """Converts a single user utterance into an ordered dict.

        Args:
            user_utterance: Original user utterance object.

        Returns:
            Dict with a user utterance.
        """
        result = OrderedDict()
        result[KEY_USER_INTENT] = user_utterance.intent["name"]

        if len(user_utterance.entities):
            entities = []
            for entity in user_utterance.entities:
                if entity["value"]:
                    entities.append(OrderedDict([(entity["entity"], entity["value"])]))
                else:
                    entities.append(entity["entity"])
            result[KEY_ENTITIES] = entities

        return result

    @staticmethod
    def process_action(action: ActionExecuted) -> OrderedDict:
        """Converts a single action into an ordered dict.

        Args:
            action: Original action object.

        Returns:
            Dict with an action.
        """
        result = OrderedDict()
        result[KEY_ACTION] = action.action_name

        return result

    @staticmethod
    def process_slot(event: SlotSet):
        """Converts a single `SlotSet` event into an ordered dict.

        Args:
            event: Original `SlotSet` event.

        Returns:
            Dict with an `SlotSet` event.
        """
        return OrderedDict([(KEY_SLOT_NAME, [{event.key: event.value}])])

    @staticmethod
    def process_checkpoints(checkpoints: List[Checkpoint]) -> List[OrderedDict]:
        """Converts checkpoints event into an ordered dict.

        Args:
            checkpoints: List of original checkpoint.

        Returns:
            List of converted checkpoints.
        """
        result = []
        for checkpoint in checkpoints:
            next_checkpoint = OrderedDict([(KEY_CHECKPOINT, checkpoint.name)])
            if checkpoint.conditions:
                next_checkpoint[KEY_CHECKPOINT_SLOTS] = [
                    {key: value} for key, value in checkpoint.conditions.items()
                ]
            result.append(next_checkpoint)
        return result

    def process_or_utterances(self, utterances: List[UserUttered]) -> OrderedDict:
        """Converts user utterance containing the `OR` statement.

        Args:
            utterances: User utterances belonging to the same `OR` statement.

        Returns:
            Dict with converted user utterances.
        """
        return OrderedDict(
            [
                (
                    KEY_OR,
                    [
                        self.process_user_utterance(utterance)
                        for utterance in utterances
                    ],
                )
            ]
        )
